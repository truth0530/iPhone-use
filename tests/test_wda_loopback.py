"""Pinned synthetic Git trees validate WDA staging without downloading/building WDA."""
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"server"))
import wda_setup


def listener_fixtures():
    return {
        "WebDriverAgentLib/Utilities/FBConfiguration.m":b'''// Synthetic fixture with the reviewed upstream binding method.
- (NSString *)bindingIPAddress
{
  // Existence of USE_IP in the environment allows specifying which interface to bind to
  if (NSProcessInfo.processInfo.environment[@"USE_IP"] &&
      [NSProcessInfo.processInfo.environment[@"USE_IP"] length] > 0) {
    return NSProcessInfo.processInfo.environment[@"USE_IP"];
  }

  return nil;
}
''',
        "WebDriverAgentLib/Routing/FBTCPSocket.m":b'''// Synthetic fixture: configuration must precede listener creation.
- (BOOL)startWithError:(NSError **)error
{
  nw_parameters_t parameters = nw_parameters_create_secure_tcp(NW_PARAMETERS_DISABLE_PROTOCOL,
                                                                NW_PARAMETERS_DEFAULT_CONFIGURATION);
  if (nil != self.interface) {
    nw_endpoint_t localEndpoint = nw_endpoint_create_host(self.interface.UTF8String, portCString);
    nw_parameters_set_local_endpoint(parameters, localEndpoint);
    listener = nw_listener_create(parameters);
  } else {
    listener = nw_listener_create_with_port(portCString, parameters);
  }
  return YES;
}
''',
    }


def build_fixtures(state,source=None):
    root=Path(state)/"derived_data"
    if source is not None:root=root/Path(source).name
    products=root/"Build/Products"
    runner=products/"Debug-iphoneos/WebDriverAgentRunner-Runner.app/WebDriverAgentRunner-Runner"
    runner.parent.mkdir(parents=True,exist_ok=True)
    runner.write_bytes(b"synthetic runner, never executed");runner.chmod(0o700)
    (products/"WebDriverAgentRunner_iphoneos.xctestrun").write_bytes(b"synthetic XCTest configuration")
    return products


@unittest.skipUnless(shutil.which("git"),"Git unavailable")
class LoopbackSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve()
        self.source=self.root/"source"
        self.source.mkdir()
        files=dict(listener_fixtures(),**{"WebDriverAgent.xcodeproj/project.pbxproj":b"synthetic project\n"})
        for name,data in files.items():
            path=self.source/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data)
        self.git("init","-q");self.git("add",".");self.git("-c","user.name=Offline test","-c","user.email=offline@example.invalid","commit","-qm","fixture")
        self.pin=patch.object(wda_setup,"WDA_COMMIT",self.git("rev-parse","HEAD").strip())
        self.pin.start();self.addCleanup(self.pin.stop)
        self.manager=wda_setup.SetupManager(self.root/"state")
        self.config={"source_dir":str(self.source),"local_port":18100}
        self.products=build_fixtures(self.manager.state_dir)

    def git(self,*args):
        return subprocess.check_output([shutil.which("git"),"-c","core.hooksPath=/dev/null","-C",str(self.source),*args],text=True)

    def marker(self,stage,policy=wda_setup.NETWORK_POLICY):
        if not (self.manager._derived_data(stage)/"Build/Products").exists():build_fixtures(self.manager.state_dir,stage)
        wda_setup._write_json(self.manager.state_dir/"build.json",{
            "config_fingerprint":wda_setup._fingerprint(self.config),"commit":wda_setup.WDA_COMMIT,
            "network_policy":policy,"loopback_source":str(stage),"artifacts_digest":self.manager._build_artifacts(stage)})

    def test_both_listeners_are_pinned_to_loopback_and_original_checkout_is_preserved(self):
        before={name:(self.source/name).read_bytes() for name in listener_fixtures()}
        stage=self.manager._loopback_source(self.config,create=True)
        http=(stage/"WebDriverAgentLib/Utilities/FBConfiguration.m").read_text()
        stream=(stage/"WebDriverAgentLib/Routing/FBTCPSocket.m").read_text()
        self.assertIn('return @"127.0.0.1";',http)
        self.assertNotIn("USE_IP",http)
        self.assertLess(stream.index('self.interface = @"127.0.0.1";'),stream.index("nw_parameters_create_secure_tcp"))
        self.assertNotIn(".git",[path.name for path in stage.iterdir()])
        self.assertEqual({name:(self.source/name).read_bytes() for name in before},before)
        self.assertEqual(self.git("status","--porcelain"),"")
        self.marker(stage)
        self.assertEqual(self.manager._require_build(self.config),stage)

    def test_old_build_policy_is_not_reusable(self):
        stage=self.manager._loopback_source(self.config,create=True)
        for policy in (None,"wildcard", "device-loopback-v0"):
            with self.subTest(policy=policy):
                self.marker(stage,policy)
                with self.assertRaisesRegex(ValueError,"build successfully"):self.manager._require_build(self.config)

    def test_new_build_marker_cannot_relabel_an_old_running_service(self):
        stage=self.manager._loopback_source(self.config,create=True)
        self.marker(stage)
        job={"config":self.config,"base_url":self.manager.base_url}
        self.assertFalse(self.manager._matching_job(job,self.config))
        job.update(network_policy=wda_setup.NETWORK_POLICY,loopback_source=str(stage),artifacts_digest=self.manager._build_artifacts())
        self.assertTrue(self.manager._matching_job(job,self.config))
        newer=self.manager._loopback_source(self.config,create=True)
        self.marker(newer)
        self.assertFalse(self.manager._matching_job(job,self.config))

    def test_changed_missing_extra_mode_or_symlink_in_staged_source_is_rejected(self):
        for change in ("bytes","missing","extra","mode","symlink"):
            with self.subTest(change=change):
                stage=self.manager._loopback_source(self.config,create=True)
                self.marker(stage)
                target=stage/"WebDriverAgent.xcodeproj/project.pbxproj"
                if change=="bytes":target.write_text("changed")
                elif change=="missing":target.unlink()
                elif change=="extra":(stage/"external.xcconfig").write_text("override")
                elif change=="mode":target.chmod(0o755)
                elif change=="symlink":target.unlink();target.symlink_to(self.source/"WebDriverAgent.xcodeproj/project.pbxproj")
                with self.assertRaises(ValueError):self.manager._require_build(self.config)
                self.assertTrue(stage.exists())

    def test_changed_patch_preimage_fails_before_creating_stage(self):
        target=self.source/"WebDriverAgentLib/Utilities/FBConfiguration.m"
        target.write_text(target.read_text().replace("return nil;","return @\"0.0.0.0\";"))
        self.git("add",".");self.git("-c","user.name=Offline test","-c","user.email=offline@example.invalid","commit","-qm","changed fixture")
        with patch.object(wda_setup,"WDA_COMMIT",self.git("rev-parse","HEAD").strip()):
            with self.assertRaisesRegex(ValueError,"patch does not match"):self.manager._loopback_source(self.config,create=True)
        self.assertEqual(list((self.manager.state_dir/"runtime").iterdir()),[])

    def test_external_or_symlinked_stage_locations_are_rejected(self):
        stage=self.manager._loopback_source(self.config,create=True)
        alias=stage.parent/"wda-loopback-alias";alias.symlink_to(stage,target_is_directory=True)
        for location in (self.source,alias):
            with self.subTest(location=location):
                self.marker(location)
                with self.assertRaisesRegex(ValueError,"location"):self.manager._require_build(self.config)


    def test_modified_added_missing_mode_or_symlink_in_built_products_is_rejected(self):
        stage=self.manager._loopback_source(self.config,create=True)
        self.products=build_fixtures(self.manager.state_dir,stage)
        for change in ("bytes","extra","missing","mode","symlink"):
            with self.subTest(change=change):
                shutil.rmtree(self.products);build_fixtures(self.manager.state_dir,stage)
                self.marker(stage)
                runner=self.products/"Debug-iphoneos/WebDriverAgentRunner-Runner.app/WebDriverAgentRunner-Runner"
                if change=="bytes":runner.write_bytes(b"changed runner")
                elif change=="extra":(runner.parent/"extra.dylib").write_bytes(b"unverified library")
                elif change=="missing":runner.unlink()
                elif change=="mode":runner.chmod(0o600)
                elif change=="symlink":runner.unlink();runner.symlink_to(self.source/"WebDriverAgent.xcodeproj/project.pbxproj")
                with self.assertRaises(ValueError):self.manager._require_build(self.config)

    def test_missing_build_products_and_unverified_old_marker_fail_closed(self):
        stage=self.manager._loopback_source(self.config,create=True)
        self.marker(stage)
        marker=wda_setup._read_json(self.manager.state_dir/"build.json")
        marker.pop("artifacts_digest");wda_setup._write_json(self.manager.state_dir/"build.json",marker)
        with self.assertRaisesRegex(ValueError,"not verified"):self.manager._require_build(self.config)
        self.marker(stage);shutil.rmtree(self.manager._derived_data(stage)/"Build/Products")
        with self.assertRaisesRegex(ValueError,"missing"):self.manager._require_build(self.config)

    def test_separate_stages_cannot_share_old_derived_data(self):
        stages=[self.manager._loopback_source(self.config,create=True) for _ in range(2)]
        config=dict(self.config,udid="synthetic-device",team_id="ABCDE12345",bundle_id="com.example.fixture",device_port=8100)
        commands=[self.manager._build_command(dict(config,source_dir=str(stage)),"build-for-testing") for stage in stages]
        roots=[Path(command[command.index("-derivedDataPath")+1]) for command in commands]
        self.assertNotEqual(roots[0],roots[1])
        self.assertTrue(all(root.parent==self.manager.state_dir/"derived_data" for root in roots))
        # Old shared products exist, but a fresh stage cannot acquire their proof.
        wda_setup._write_json(self.manager.state_dir/"build.json",{"loopback_source":str(stages[1])})
        with self.assertRaisesRegex(ValueError,"missing"):self.manager._build_artifacts()


if __name__=="__main__":unittest.main()
