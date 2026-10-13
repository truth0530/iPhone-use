"""Real loopback/Git and isolated Node regressions, without WDA or USB access."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"server"))
import wda_setup
from wda_client import SESSION_SETTINGS,WDAClient,WDAError
from wda_controller import predicate
from test_client import fake_server


class ObservationTransportTests(unittest.TestCase):
    def test_query_grammar_accepts_escaped_data_and_rejects_expressions(self):
        for label in ("Target","FUNCTION(SELF, 'description')","a' OR TRUEPREDICATE", "back\\slash\nline\u2028"):
            generated=predicate({"label":label,"type":"Button","enabled":True})
            self.assertTrue(WDAClient.allowed_observation_request("POST","/session/sid/elements",{"using":"predicate string","value":generated}))
        for expression in ("TRUEPREDICATE","FUNCTION(SELF, 'description') != nil","label == 'X' OR enabled == true","label == 'X' AND SELF.description != nil"):
            self.assertFalse(WDAClient.allowed_observation_request("POST","/session/sid/elements",{"using":"predicate string","value":expression}))
    def test_mutating_unknown_and_session_replacement_routes_never_reach_network(self):
        with fake_server(lambda *args:(200,{"value":True},0,False)) as (url,requests,_):
            client=WDAClient(url,read_only=True)
            try:
                routes=[("POST","/wda/keys",{"value":["synthetic"]}),
                        ("POST","/session/sid/element/target/click",{}),
                        ("POST","/wda/homescreen",{}),("GET","/wda/healthcheck",None),
                        ("GET","/unknown",None),("DELETE","/session/sid",None),
                        ("POST","/session",{"capabilities":{"alwaysMatch":{"shouldTerminateApp":False}}}),
                        ("POST","/session/sid/appium/settings",{"settings":{"defaultActiveApplication":"com.other"}}),
                        ("POST","/session/sid/elements",{"using":"xpath","value":"//*"})]
                for method,path,payload in routes:
                    with self.subTest(path=path),self.assertRaises(WDAError) as caught:
                        client.request(method,path,payload)
                    self.assertEqual(caught.exception.code,"safety_policy")
                    self.assertFalse(caught.exception.details["action_executed"])
                with self.assertRaises(WDAError):client.ensure_session()
                self.assertEqual(requests,[])
            finally:client.close()

    def test_existing_session_observations_and_fixed_settings_still_work(self):
        with fake_server(lambda *args:(200,{"value":[]},0,False)) as (url,requests,_):
            client=WDAClient(url,read_only=True)
            client.session_id="manual-session"
            try:
                self.assertEqual(client.session("GET","/source?format=xml"),[])
                self.assertEqual(client.session("POST","/elements",{"using":"predicate string","value":"label == 'Target'"}),[])
                self.assertEqual([item[:2] for item in requests],[
                    ("POST","/session/manual-session/appium/settings"),
                    ("GET","/session/manual-session/source?format=xml"),
                    ("POST","/session/manual-session/elements")])
                self.assertEqual(requests[0][2],{"settings":dict(SESSION_SETTINGS)})
            finally:client.close()


@unittest.skipUnless(shutil.which("git"),"Git unavailable")
class PinnedSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.source=Path(self.temp.name)/"source"
        self.source.mkdir()
        self.project=self.source/"WebDriverAgent.xcodeproj/project.pbxproj"
        self.project.parent.mkdir()
        self.project.write_text("synthetic project\n")
        (self.source/".gitignore").write_text("ignored-config\n")
        self.git("init","-q")
        self.git("add",".")
        self.git("-c","user.name=Offline test","-c","user.email=offline@example.invalid","commit","-qm","synthetic fixture")
        commit=self.git("rev-parse","HEAD").strip()
        pin=patch.object(wda_setup,"WDA_COMMIT",commit)
        pin.start();self.addCleanup(pin.stop)
        self.manager=wda_setup.SetupManager(Path(self.temp.name)/"state")
        self.config={"source_dir":str(self.source)}

    def git(self,*args):
        return subprocess.check_output([shutil.which("git"),"-c","core.hooksPath=/dev/null","-c","core.fsmonitor=false","-C",str(self.source),*args],text=True)

    def test_pristine_real_checkout_is_accepted(self):
        self.assertEqual(self.manager._source(self.config),self.source.resolve())

    def test_assume_unchanged_and_skip_worktree_cannot_hide_modified_bytes(self):
        for flag in ("--assume-unchanged","--skip-worktree"):
            with self.subTest(flag=flag):
                self.project.write_text("synthetic project\n")
                self.git("update-index",flag,"WebDriverAgent.xcodeproj/project.pbxproj")
                self.project.write_text("hidden modification\n")
                self.assertEqual(self.git("status","--porcelain","--untracked-files=no"),"")
                with self.assertRaisesRegex(ValueError,"bytes.*changed"):self.manager._source(self.config)
                self.assertEqual(self.project.read_text(),"hidden modification\n")
                self.git("update-index","--no-assume-unchanged","--no-skip-worktree","WebDriverAgent.xcodeproj/project.pbxproj")

    def test_untracked_and_ignored_files_are_rejected_and_preserved(self):
        for name in ("untracked.xcconfig","ignored-config"):
            extra=self.source/name
            extra.write_text("synthetic override\n")
            with self.subTest(name=name),self.assertRaisesRegex(ValueError,"Unexpected"):self.manager._source(self.config)
            self.assertEqual(extra.read_text(),"synthetic override\n")
            extra.unlink()

    def test_symlink_and_executable_mode_changes_are_rejected(self):
        self.project.chmod(0o755)
        with self.assertRaisesRegex(ValueError,"executable mode"):self.manager._source(self.config)
        self.project.chmod(0o644)
        outside=Path(self.temp.name)/"outside"
        outside.write_bytes(self.project.read_bytes())
        self.project.unlink();self.project.symlink_to(outside)
        with self.assertRaisesRegex(ValueError,"unsupported"):self.manager._source(self.config)


@unittest.skipUnless(shutil.which("node"),"Node unavailable")
class ToolingBoundaryTests(unittest.TestCase):
    def test_usb_override_is_rejected_before_library_import_or_socket_creation(self):
        harness="""
import vm from 'node:vm';
let input='';for await(const chunk of process.stdin)input+=chunk;
const {source,address}=JSON.parse(input);const events=[];
const context=vm.createContext({process:{env:{USBMUXD_SOCKET_ADDRESS:address},argv:['node','fixture','TEST-DEVICE-1234'],exit(){throw Error('exit');}},console:{}});
const module=new vm.SourceTextModule(source,{context,importModuleDynamically(){events.push('library import');throw Error('forbidden import');}});
await module.link(specifier=>new vm.SyntheticModule(['default'],function(){this.setExport('default',new Proxy({},{get(){events.push('socket use');throw Error('forbidden socket');}}));},{context}));
let rejected=false;try{await module.evaluate();}catch{rejected=true;}
process.stdout.write(JSON.stringify({rejected,events}));
"""
        for script in ("forward.mjs","screen-stream.mjs"):
            for address in ("outside.invalid:27015","[2001:db8::1]:27015","127.0.0.1:27015","/tmp/other-usbmuxd"):
                with self.subTest(script=script,address=address):
                    result=subprocess.run([shutil.which("node"),"--experimental-vm-modules","--input-type=module","-e",harness],
                                          input=json.dumps({"source":(ROOT/"tooling"/script).read_text(),"address":address}),text=True,capture_output=True,check=True,timeout=5)
                    self.assertEqual(json.loads(result.stdout),{"rejected":True,"events":[]})

    @unittest.skipUnless(shutil.which("npm"),"npm unavailable")
    def test_lock_override_resolves_every_shell_quote_consumer_to_patched_version(self):
        package=json.loads((ROOT/"tooling/package.json").read_text())
        lock=json.loads((ROOT/"tooling/package-lock.json").read_text())
        self.assertEqual(package["overrides"]["shell-quote"],"1.11.0")
        entries=[item for name,item in lock["packages"].items() if name.endswith("node_modules/shell-quote")]
        self.assertEqual(len(entries),1)
        self.assertEqual(entries[0]["version"],"1.11.0")
        self.assertEqual(entries[0]["integrity"],"sha512-JdxDPD0DBTyu08pq0kPC0xSNet/qsU07qT6IsX1AS8oO2ICNRY4ldNa8OAI6PuwAH8tG3lxEhbqmyp4Dw4036g==")
        result=subprocess.run([shutil.which("npm"),"ls","--package-lock-only","--prefix",str(ROOT/"tooling"),"shell-quote","--json"],text=True,capture_output=True,check=True,timeout=10)
        def versions(node):
            for name,child in node.get("dependencies",{}).items():
                if name=="shell-quote":yield child["version"]
                yield from versions(child)
        self.assertEqual(set(versions(json.loads(result.stdout))),{"1.11.0"})
