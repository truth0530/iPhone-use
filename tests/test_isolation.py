"""Exercise the real macOS process sandbox; never connect to USB or WDA."""
import errno
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"server"))
import isolated
from test_client import fake_server


@unittest.skipUnless(sys.platform=="darwin" and Path("/usr/bin/sandbox-exec").is_file(),"macOS sandbox-exec required")
class IsolationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve()
        self.state=self.root/"state"

    def probe(self,code,url=None):
        command,environment=isolated.launch_parameters(self.state,url)
        return subprocess.run(command[:3]+[str(isolated.python_executable()),"-I","-S","-B","-c",code],
                              env=environment,capture_output=True,text=True,timeout=10,check=True)

    def broker_probe(self,code,url):
        command,environment=isolated.launch_parameters(self.state,url)
        command=command[:3]+[str(isolated.python_executable()),"-I","-S","-B","-c",
                            "import sys;sys.path.insert(0,"+repr(str(ROOT/"server"))+");"+code]
        wrapper="import sys;sys.path.insert(0,"+repr(str(ROOT/"server"))+");import isolated;sys.exit(isolated.run_isolated("+repr(command)+","+repr(environment)+"))"
        return subprocess.run([str(isolated.python_executable()),"-I","-S","-B","-c",wrapper],
                              capture_output=True,text=True,timeout=10,check=True)

    def test_real_server_initializes_serves_widget_and_reads_local_catalog(self):
        calls=[("initialize",{}),("resources/read",{"uri":"ui://iphone-use/phone-0.3.10.html"}),
               ("tools/call",{"name":"pua_apps","arguments":{"query":"Settings","source":"catalog"}})]
        wire="".join(json.dumps({"jsonrpc":"2.0","id":index,"method":method,"params":params})+"\n"
                     for index,(method,params) in enumerate(calls,1))
        result=subprocess.run([sys.executable,"-I","-S","-B",str(ROOT/"server/isolated.py"),"--state-dir",str(self.state)],
                              input=wire,text=True,capture_output=True,timeout=10,check=True)
        replies=[json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(len(replies),3)
        self.assertTrue(all("error" not in reply for reply in replies),result.stderr)
        self.assertIn("read-only",replies[0]["result"]["instructions"])
        self.assertIn("untrusted data",replies[0]["result"]["instructions"])
        self.assertNotIn("scripts/phone.py",replies[0]["result"]["instructions"])
        self.assertIn("<html",replies[1]["result"]["contents"][0]["text"].lower())
        self.assertFalse(replies[2]["result"].get("isError",False),str(replies[2]))

    def test_kernel_denies_all_direct_network_including_selected_wda_port(self):
        with fake_server(lambda *args:(200,{"value":True},0,False)) as (allowed,requests,_), \
             fake_server(lambda *args:(200,{"value":True},0,False)) as (other,other_requests,_):
            other_port=int(other.rsplit(":",1)[1])
            code=f'''import socket,errno,json
results=[]
for host,port in [("127.0.0.1",{int(allowed.rsplit(':',1)[1])}),("127.0.0.1",{other_port}),("192.0.2.1",9)]:
 s=socket.socket();s.settimeout(1)
 try:s.connect((host,port));results.append(0)
 except OSError as error:results.append(error.errno)
 finally:s.close()
print(json.dumps(results))'''
            result=self.probe(code,allowed)
            self.assertEqual(json.loads(result.stdout),[errno.EPERM]*3)
            self.assertEqual(requests,[])
            self.assertEqual(other_requests,[])

    def test_broker_kernel_allows_only_fixed_port_and_denies_mcp_state_access(self):
        with fake_server(lambda *args:(200,{"value":True},0,False)) as (allowed,requests,_), \
             fake_server(lambda *args:(200,{"value":True},0,False)) as (other,other_requests,_):
            isolated.launch_parameters(self.state,allowed)
            session=self.state/"session.json";session.write_text("synthetic state")
            code=f'''import urllib.request,socket,json
from pathlib import Path
assert urllib.request.urlopen({allowed!r}+"/status",timeout=1).status==200
results=[]
for host,port in [("127.0.0.1",{int(other.rsplit(':',1)[1])}),("192.0.2.1",9)]:
 s=socket.socket();s.settimeout(1)
 try:s.connect((host,port));results.append(0)
 except OSError as error:results.append(error.errno)
 finally:s.close()
for action in [lambda:Path({str(session)!r}).read_text(),lambda:Path({str(self.state/'operation.lock')!r}).write_text("blocked")]:
 try:action();results.append(0)
 except OSError as error:results.append(error.errno)
print(json.dumps(results))'''
            command=["/usr/bin/sandbox-exec","-p",isolated.sandbox_profile(self.state,int(allowed.rsplit(':',1)[1])),
                     str(isolated.python_executable()),"-I","-S","-B","-c",code]
            result=subprocess.run(command,env={"IPHONE_USE_ANALYTICS":"0","DO_NOT_TRACK":"1"},
                                  capture_output=True,text=True,timeout=10,check=True)
            self.assertEqual(json.loads(result.stdout),[errno.EPERM]*4)
            self.assertEqual(len(requests),1);self.assertEqual(other_requests,[])

    def test_real_server_can_read_phone_tree_through_allowed_port_and_manual_session(self):
        def respond(method,path,payload,requests):
            if path=="/status":value={"ready":True}
            elif path=="/wda/locked":value=False
            elif path.endswith("/window/size"):value={"width":393,"height":852}
            elif path.startswith("/session/manual-session/source"):
                value='<XCUIElementTypeApplication type="XCUIElementTypeApplication" bundleId="com.example.fixture" x="0" y="0" width="393" height="852"><XCUIElementTypeStaticText type="XCUIElementTypeStaticText" label="Synthetic content" x="0" y="0" width="100" height="30"/></XCUIElementTypeApplication>'
            elif path=="/wda/activeAppInfo":value={"bundleId":"com.example.fixture"}
            else:value={}
            return 200,{"value":value},0,False
        with fake_server(respond) as (url,requests,_):
            command,environment=isolated.launch_parameters(self.state,url)
            (self.state/"session.json").write_text(json.dumps({"url":url,"session_id":"manual-session"}))
            wire=json.dumps({"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"pua_observe","arguments":{"mode":"tree"}}})+"\n"
            result=subprocess.run([sys.executable,"-I","-S","-B",str(ROOT/"server/isolated.py"),"--state-dir",str(self.state),"--url",url],
                                  input=wire,text=True,capture_output=True,timeout=10,check=True)
            reply=json.loads(result.stdout)
            self.assertFalse(reply["result"].get("isError",False),str(reply))
            self.assertIn("Synthetic content",result.stdout)
            self.assertTrue(any("/source" in path for _,path,_ in requests))
            self.assertTrue(all(method=="GET" or path.endswith("/appium/settings") for method,path,_ in requests))

    def test_broker_enforces_policy_even_with_disabled_client_guard_and_private_method(self):
        with fake_server(lambda *args:(200,{"value":True},0,False)) as (url,requests,_):
            code=f'''import json,socket,errno
from wda_client import WDAClient,WDAError
client=WDAClient({url!r},read_only=False)
assert client.request("GET","/status")=={{"value":True}}
codes=[]
for method,path,payload in [("POST","/wda/keys",{{"value":["synthetic"]}}),("POST","/session",{{}}),("GET","/wda/healthcheck",None),("GET","/status?arbitrary=true",None),("GET","/session/sid/../wda/homescreen",None),("GET","/source%3Fformat=xml",None)]:
 try:client._request_once(method,path,payload,2,True);codes.append("accepted")
 except WDAError as error:codes.append(error.code)
s=socket.socket()
try:s.connect(("127.0.0.1",{int(url.rsplit(':',1)[1])}));codes.append(0)
except OSError as error:codes.append(error.errno)
finally:s.close()
print(json.dumps(codes))'''
            result=self.broker_probe(code,url)
            self.assertEqual(json.loads(result.stdout),["safety_policy"]*6+[errno.EPERM])
            self.assertEqual([item[:2] for item in requests],[("GET","/status")])

    def test_broker_rejects_destination_and_invalid_budget_in_forged_channel_messages(self):
        with fake_server(lambda *args:(200,{"value":True},0,False)) as (url,requests,_):
            code='''import os,socket,json
from observation_broker import send,receive,REQUEST_LIMIT,RESPONSE_LIMIT
channel=socket.socket(fileno=os.dup(int(os.environ["IPHONE_USE_BROKER_FD"])))
base={"method":"GET","path":"/status","payload":None,"budget":2}
messages=[dict(base,url="http://outside.invalid"),dict(base,budget=True),dict(base,budget=31),dict(base,path="http://outside.invalid/status"),dict(base,path="/status\\r\\nInjected: value")]
codes=[]
for message in messages:
 send(channel,message,REQUEST_LIMIT);codes.append(receive(channel,RESPONSE_LIMIT)["error"]["code"])
print(json.dumps(codes))'''
            result=self.broker_probe(code,url)
            self.assertEqual(json.loads(result.stdout),["safety_policy"]*5)
            self.assertEqual(requests,[])

    def test_missing_broker_channel_never_falls_back_to_http(self):
        with fake_server(lambda *args:(200,{"value":True},0,False)) as (url,requests,_):
            code=f'''import os,json
from wda_client import WDAClient,WDAError
os.environ["IPHONE_USE_BROKER_FD"]="999999"
try:WDAClient({url!r},read_only=False).request("GET","/status")
except WDAError as error:print(json.dumps(error.code))'''
            result=self.broker_probe(code,url)
            self.assertEqual(json.loads(result.stdout),"isolation_unavailable")
            self.assertEqual(requests,[])

    def test_screenshot_artifacts_pause_state_and_service_status_work_under_restricted_writes(self):
        png="iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jXuoAAAAASUVORK5CYII="
        def respond(method,path,payload,requests):
            if path=="/status":value={"ready":True}
            elif path=="/wda/locked":value=False
            elif path.endswith("/window/size"):value={"width":393,"height":852}
            elif path=="/wda/activeAppInfo":value={"bundleId":"com.example.fixture"}
            elif path=="/screenshot":value=png
            else:value={}
            return 200,{"value":value},0,False
        with fake_server(respond) as (url,requests,_):
            code=f'''from pathlib import Path
import json
from iphone_use import Runtime
r=Runtime({str(self.state)!r},{url!r})
r.screen.set_paused(True)
assert r.screen.paused()
r.screen.set_paused(False)
assert not r.screen.paused()
r.client.session_id="manual-session"
image=r.phone.observe(mode="screenshot")["image"]
assert Path(image["path"]).read_bytes().startswith(b"\\x89PNG")
assert r.setup_manager._probe_status()["ready"]
assert r.client.records
print(json.dumps(True))'''
            result=self.broker_probe(code,url)
            self.assertTrue(json.loads(result.stdout))
            self.assertTrue(any(path=="/screenshot" for _,path,_ in requests))

    def test_kernel_denies_shell_fork_usb_socket_and_outside_files(self):
        secret=self.root/"secret"
        secret.write_text("synthetic private data")
        outside=self.root/"outside"
        state_file=self.state/"artifacts/allowed"
        self.state.mkdir()
        state_file.parent.mkdir()
        (self.state/"escape").symlink_to(outside)
        # A synthetic UNIX listener proves even readable state sockets are
        # blocked. Never probe the real usbmux daemon or any USB device.
        unix_path=self.state/"synthetic-usbmux"
        unix_listener=socket.socket(socket.AF_UNIX)
        self.addCleanup(unix_listener.close)
        unix_listener.bind(str(unix_path));unix_listener.listen(1)
        code=f'''import os,socket,subprocess,json
from pathlib import Path
Path({str(state_file)!r}).write_text("local result")
results=[]
actions=[lambda:subprocess.run(["/bin/sh","-c","exit 0"],check=True),lambda:os.fork(),
lambda:socket.socket(socket.AF_UNIX).connect({str(unix_path)!r}),
lambda:Path({str(secret)!r}).read_text(),lambda:Path({str(outside)!r}).write_text("blocked"),
lambda:Path({str(self.state/'escape')!r}).write_text("blocked"),
lambda:Path({str(ROOT/'server/iphone_use.py')!r}).open("a")]
for action in actions:
 try:action();results.append(0)
 except OSError as error:results.append(error.errno)
print(json.dumps(results))'''
        result=self.probe(code)
        self.assertEqual(json.loads(result.stdout),[errno.EPERM]*7)
        self.assertEqual(state_file.read_text(),"local result")
        self.assertFalse(outside.exists())

    def test_kernel_denies_modifying_lifecycle_configuration_binaries_and_forward_code(self):
        isolated.launch_parameters(self.state)
        names=("config.json","build.json","runtime/forward/forward.mjs","runtime/wda-loopback-fixture/source.m",
               "derived_data/Build/Products/runner.app/runner","jobs/fixture.json","logs/fixture.log")
        paths=[self.state/name for name in names]
        for path in paths:path.parent.mkdir(parents=True,exist_ok=True);path.write_text("{}")
        # Artifacts cannot serve as a symlink route into protected build files.
        (self.state/"artifacts").mkdir()
        (self.state/"artifacts/escape").symlink_to(paths[4])
        code=f'''from pathlib import Path
import json
results=[]
for path in {list(map(str,paths))+[str(self.state/'artifacts/escape')]!r}:
 try:Path(path).write_text("tampered");results.append(0)
 except OSError as error:results.append(error.errno)
try:__import__("os").link({str(paths[4])!r},{str(self.state/'artifacts/hardlink')!r});results.append(0)
except OSError as error:results.append(error.errno)
for path in {list(map(str,[self.state,self.state/'runtime',self.state/'jobs',self.state/'logs']))!r}:
 try:Path(path).chmod(0o777);results.append(0)
 except OSError as error:results.append(error.errno)
print(json.dumps(results))'''
        # Call the sandbox directly: production launch additionally rejects
        # preexisting writable symlinks before starting either child.
        environment={"IPHONE_USE_ANALYTICS":"0","DO_NOT_TRACK":"1"}
        result=subprocess.run(["/usr/bin/sandbox-exec","-p",isolated.sandbox_profile(self.state),
                               str(isolated.python_executable()),"-I","-S","-B","-c",code],
                              env=environment,capture_output=True,text=True,timeout=10,check=True)
        self.assertEqual(json.loads(result.stdout),[errno.EPERM]*13)
        self.assertTrue(all(path.read_text()=="{}" for path in paths))

    def test_private_state_files_and_build_material_are_unreadable_from_mcp(self):
        isolated.launch_parameters(self.state)
        secret=self.state/"unrelated-secret"
        build=self.state/"runtime/private-build-material"
        secret.write_text("synthetic secret");build.write_text("synthetic build")
        code=f'''from pathlib import Path
import json
results=[]
for path in {list(map(str,[secret,build]))!r}:
 try:Path(path).read_text();results.append(0)
 except OSError as error:results.append(error.errno)
print(json.dumps(results))'''
        self.assertEqual(json.loads(self.probe(code).stdout),[errno.EPERM]*2)

    def test_preexisting_writable_symlinks_and_hard_links_fail_before_launch(self):
        isolated.launch_parameters(self.state)
        trusted=self.state/"build.json";trusted.write_text("{}")
        alias=self.state/"session.json"
        alias.symlink_to(trusted)
        with self.assertRaisesRegex(ValueError,"symlinks"):isolated.launch_parameters(self.state)
        alias.unlink();os.link(trusted,alias)
        with self.assertRaisesRegex(ValueError,"hard links"):isolated.launch_parameters(self.state)
        self.assertEqual(trusted.read_text(),"{}")

    def test_session_temp_hardlink_cannot_truncate_its_target(self):
        isolated.launch_parameters(self.state)
        leftover=self.state/"session-3.tmp"
        leftover.write_text("old")
        isolated.launch_parameters(self.state)
        self.assertEqual(leftover.read_text(),"old")
        secret=self.root/"secret"
        secret.write_text("SAFE")
        other=self.root/"other"
        other.write_text("OTHER")
        linked=self.root/"linked"
        linked.write_text("LINKED")
        planted=self.state/"session-4242.tmp"
        os.link(secret,planted)
        os.link(other,self.state/".screen-trap")
        (self.state/"session-7.tmp").symlink_to(linked)
        with self.assertRaisesRegex(ValueError,"symlinks|hard links"):isolated.launch_parameters(self.state)
        kept=self.root/"kept"
        kept.write_text("KEEP")
        os.link(kept,self.state/"session.json")
        code=f'''import os,sys
sys.path.insert(0,{str(ROOT/"server")!r})
from iphone_use import write_exclusive_text
created=write_exclusive_text({str(self.state)!r},"fresh-session")
extra=write_exclusive_text({str(self.state)!r},"created")
os.replace(created,{str(self.state/"session.json")!r})
print(extra)
'''
        environment={"IPHONE_USE_ANALYTICS":"0","DO_NOT_TRACK":"1","PATH":"/usr/bin:/bin","HOME":str(Path.home()),"LC_CTYPE":"UTF-8"}
        result=subprocess.run(["/usr/bin/sandbox-exec","-p",isolated.sandbox_profile(self.state),
                               str(isolated.python_executable()),"-I","-S","-B","-c",code],
                              env=environment,capture_output=True,text=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stderr)
        extra=Path(result.stdout.strip())
        self.assertEqual(extra.read_text(),"created")
        self.assertEqual((self.state/"session.json").read_text(),"fresh-session")
        self.assertNotEqual((self.state/"session.json").stat().st_ino,kept.stat().st_ino)
        self.assertEqual(kept.read_text(),"KEEP")
        self.assertEqual(kept.stat().st_nlink,1)
        self.assertEqual(secret.read_text(),"SAFE")
        self.assertEqual(secret.stat().st_nlink,2)
        self.assertEqual(planted.read_text(),"SAFE")
        self.assertEqual(other.read_text(),"OTHER")
        self.assertEqual(linked.read_text(),"LINKED")

    def test_job_and_log_hardlinks_do_not_expose_outside_bytes(self):
        isolated.launch_parameters(self.state)
        marker="synthetic-secret"
        secret=self.root/"secret.json"
        secret.write_text('{"id":"'+("c"*32)+'","token":"'+marker+'"}')
        planted=self.state/"jobs"/("d"*32+".json")
        os.link(secret,planted)
        log_secret=self.root/"log-secret"
        log_secret.write_text("LOG-SECRET-BYTES")
        os.link(log_secret,self.state/"logs"/("e"*32+".log"))
        plain=self.state/"jobs"/("a"*32+".json")
        plain.write_text('{"id":"'+("a"*32)+'","state":"failed"}')
        (self.state/"logs"/("f"*32+".log")).write_text("ordinary-log")
        with self.assertRaisesRegex(ValueError,"hard links"):isolated.launch_parameters(self.state)
        code=f'''import json,sys
sys.path.insert(0,{str(ROOT/"server")!r})
from pathlib import Path
from wda_setup import _read_json
print(json.dumps({{"raw":Path({str(planted)!r}).read_text(),"policy":_read_json({str(planted)!r}),"plain":_read_json({str(plain)!r})}}))
'''
        environment={"IPHONE_USE_ANALYTICS":"0","DO_NOT_TRACK":"1","PATH":"/usr/bin:/bin","HOME":str(Path.home()),"LC_CTYPE":"UTF-8"}
        result=subprocess.run(["/usr/bin/sandbox-exec","-p",isolated.sandbox_profile(self.state),
                               str(isolated.python_executable()),"-I","-S","-B","-c",code],
                              env=environment,capture_output=True,text=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stderr)
        payload=json.loads(result.stdout)
        self.assertIn(marker,payload["raw"])
        self.assertIsNone(payload["policy"])
        self.assertEqual(payload["plain"]["state"],"failed")
        from wda_setup import SetupManager
        manager=SetupManager(self.state,"http://127.0.0.1:18100")
        self.assertNotIn("LOG-SECRET-BYTES",manager._job_status({"id":"e"*32,"state":"failed"})["log_tail"])
        self.assertIn("ordinary-log",manager._job_status({"id":"f"*32,"state":"failed"})["log_tail"])
        self.assertEqual(log_secret.read_text(),"LOG-SECRET-BYTES")
        self.assertIn(marker,secret.read_text())

    def test_config_device_and_lock_aliases_do_not_change_or_expose_targets(self):
        isolated.launch_parameters(self.state)
        secret=self.root/"config-secret"
        secret.write_text('{"local_port":18100,"token":"synthetic-secret"}')
        secret.chmod(0o644)
        os.link(secret,self.state/"config.json")
        os.link(secret,self.state/"device.json")
        os.link(secret,self.state/"build.json")
        lock_target=self.root/"lock-target"
        lock_target.write_text("LOCK")
        lock_target.chmod(0o644)
        os.link(lock_target,self.state/"operation.lock")
        with self.assertRaisesRegex(ValueError,"hard links"):isolated.launch_parameters(self.state)
        code=f'''import json,os,sys
sys.path.insert(0,{str(ROOT/"server")!r})
from pathlib import Path
from wda_setup import open_unaliased, read_exclusive_text
raw=Path({str(self.state/"config.json")!r}).read_text()
refused=None
try:open_unaliased({str(self.state/"operation.lock")!r},os.O_RDWR|os.O_CREAT|os.O_APPEND)
except OSError as error:refused=error.errno
print(json.dumps({{"raw":raw,"policy":read_exclusive_text({str(self.state/"config.json")!r}),"errno":refused}}))
'''
        environment={"IPHONE_USE_ANALYTICS":"0","DO_NOT_TRACK":"1","PATH":"/usr/bin:/bin","HOME":str(Path.home()),"LC_CTYPE":"UTF-8"}
        result=subprocess.run(["/usr/bin/sandbox-exec","-p",isolated.sandbox_profile(self.state),
                               str(isolated.python_executable()),"-I","-S","-B","-c",code],
                              env=environment,capture_output=True,text=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stderr)
        payload=json.loads(result.stdout)
        self.assertIn("synthetic-secret",payload["raw"])
        self.assertIsNone(payload["policy"])
        self.assertEqual(payload["errno"],errno.EPERM)
        self.assertEqual(secret.read_text(),'{"local_port":18100,"token":"synthetic-secret"}')
        self.assertEqual(secret.stat().st_mode&0o777,0o644)
        self.assertEqual(lock_target.read_text(),"LOCK")
        self.assertEqual(lock_target.stat().st_mode&0o777,0o644)
        # standard mode writes device.json outside this sandbox. Replace must not truncate a hardlink.
        from iphone_use import replace_exclusive_text
        kept=self.root/"kept-device"
        kept.write_text("DEVICE")
        alias=self.state/"device-alias.json"
        os.link(kept,alias)
        replace_exclusive_text(alias,'{"model":"fixture"}')
        self.assertEqual(alias.read_text(),'{"model":"fixture"}')
        self.assertEqual(kept.read_text(),"DEVICE")
        self.assertEqual(kept.stat().st_nlink,1)
        self.assertNotEqual(alias.stat().st_ino,kept.stat().st_ino)

    def test_fifo_read_returns_and_setup_lock_does_not_chmod_a_hardlink(self):
        isolated.launch_parameters(self.state)
        fifo=self.root/"probe.fifo"
        os.mkfifo(fifo)
        from wda_setup import SetupManager, read_exclusive_text
        def expired(signum,frame):raise TimeoutError("fifo open blocked")
        previous=signal.signal(signal.SIGALRM,expired)
        signal.alarm(2)
        try:self.assertIsNone(read_exclusive_text(fifo))
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM,previous)
        target=self.root/"lock-secret"
        target.write_text("SECRET")
        target.chmod(0o644)
        os.link(target,self.state/".lock")
        manager=SetupManager(self.state,"http://127.0.0.1:18100")
        with self.assertRaises(OSError):
            with manager._lock():pass
        self.assertEqual(target.read_text(),"SECRET")
        self.assertEqual(target.stat().st_mode&0o777,0o644)

    def test_lifecycle_directory_symlink_is_rejected_without_changing_target_permissions(self):
        self.state.mkdir()
        outside=self.root/"outside-directory";outside.mkdir();outside.chmod(0o755)
        (self.state/"runtime").symlink_to(outside,target_is_directory=True)
        with self.assertRaisesRegex(ValueError,"lifecycle directories"):
            isolated.launch_parameters(self.state,"http://127.0.0.1:18100")
        self.assertEqual(outside.stat().st_mode&0o777,0o755)

    def test_environment_injection_cannot_disable_policy_or_load_extra_python(self):
        poison={"IPHONE_USE_SAFETY_MODE":"standard","IPHONE_USE_ANALYTICS":"1","DO_NOT_TRACK":"0",
                "NODE_OPTIONS":"--require=evil","PYTHONPATH":"/tmp/evil","USBMUXD_SOCKET_ADDRESS":"outside.invalid:27015",
                "HTTPS_PROXY":"http://outside.invalid","PYTHONSTARTUP":"/tmp/evil.py"}
        with patch.dict(os.environ,poison):command,environment=isolated.launch_parameters(self.state)
        self.assertEqual(environment["IPHONE_USE_SAFETY_MODE"],"read-only")
        self.assertEqual(environment["IPHONE_USE_ANALYTICS"],"0")
        self.assertEqual(environment["DO_NOT_TRACK"],"1")
        for name in poison.keys()-{"IPHONE_USE_SAFETY_MODE","IPHONE_USE_ANALYTICS","DO_NOT_TRACK"}:self.assertNotIn(name,environment)
        self.assertIn("-I",command);self.assertIn("-S",command);self.assertIn("-B",command)

    def test_invalid_configuration_and_unsupported_platform_fail_closed(self):
        with patch.object(isolated.sys,"platform","linux"),self.assertRaisesRegex(ValueError,"no unsandboxed fallback"):
            isolated.launch_parameters(self.state)
        for url in ("https://127.0.0.1:18100","http://outside.invalid","http://127.0.0.1:18100/path"):
            with self.subTest(url=url),self.assertRaises(ValueError):isolated.launch_parameters(self.state,url)
        with self.assertRaises(ValueError):isolated.launch_parameters(ROOT/"unsafe-state")
        for state in (ROOT.parent,Path.home(),Path("/")):
            with self.subTest(state=state),self.assertRaises(ValueError):isolated.launch_parameters(state)
        self.assertFalse((ROOT/"unsafe-state").exists())


if __name__=="__main__":unittest.main()
