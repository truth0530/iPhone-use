"""Use the installed MCP SDK against the real isolated stdio entry; never register a plugin."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
SDK=ROOT/"ui/node_modules/@modelcontextprotocol/sdk/dist/esm/client"
sys.path.insert(0,str(ROOT/"server"))
import isolated
from test_client import fake_server


@unittest.skipUnless(sys.platform=="darwin" and shutil.which("node") and (SDK/"stdio.js").is_file(),
                     "Installed MCP SDK, Node and macOS required")
class SDKConnectionTests(unittest.TestCase):
    def test_sdk_handshake_reconnect_read_tools_and_widget_mutation_rejection(self):
        def respond(method,path,payload,requests):
            if path=="/status":value={"ready":True}
            elif path=="/wda/locked":value=False
            elif path.endswith("/window/size"):value={"width":393,"height":852}
            elif "/source" in path:
                value='<XCUIElementTypeApplication type="XCUIElementTypeApplication" bundleId="com.example.fixture" x="0" y="0" width="393" height="852"><XCUIElementTypeStaticText type="XCUIElementTypeStaticText" label="Synthetic screen content" x="0" y="0" width="100" height="30"/></XCUIElementTypeApplication>'
            elif path=="/wda/activeAppInfo":value={"bundleId":"com.example.fixture"}
            else:value={}
            return 200,{"value":value},0,False
        with tempfile.TemporaryDirectory() as task_temp,fake_server(respond) as (url,requests,_):
            state=Path(task_temp).resolve()/"state"
            isolated.launch_parameters(state,url)
            (state/"session.json").write_text(json.dumps({"url":url,"session_id":"manual-session"}));(state/"session.json").chmod(0o600)
            config={"command":str(isolated.python_executable()),
                    "args":["-I","-S","-B",str(ROOT/"server/isolated.py"),"--state-dir",str(state),"--url",url],
                    "cwd":str(ROOT),"env":{"IPHONE_USE_ANALYTICS":"0","DO_NOT_TRACK":"1"},"stderr":"pipe"}
            program='''import assert from "node:assert/strict";
import {Client} from CLIENT;
import {StdioClientTransport} from STDIO;
const config=CONFIG;
const data=reply=>reply.structuredContent ?? JSON.parse(reply.content.find(block=>block.type==="text").text);
const mutations=[
 ["pua_tap",{x:1,y:1}], ["pua_type_text",{text:"synthetic"}],
 ["pua_swipe",{direction:"up"}], ["pua_launch_app",{bundle_id:"com.example.fixture"}],
 ["pua_press_button",{name:"home"}], ["pua_setup",{action:"start"}],
 ["pua_screen_action",{action:"home"}], ["pua_screen_action",{action:"screenshot"}]
];
for(let connection=0;connection<2;connection++){
 const client=new Client({name:"local-safety-test",version:"1.0.0"});
 const transport=new StdioClientTransport(config);
 try{
  await client.connect(transport);
  assert.equal(client.getServerVersion().name,"iphone-use");
  const listed=await client.listTools();
  assert(listed.tools.some(tool=>tool.name==="pua_observe"));
  const widget=await client.readResource({uri:"ui://iphone-use/phone-0.3.10.html"});
  const csp=widget.contents[0]._meta.ui.csp;
  assert.deepEqual(csp.connectDomains,[]);assert.deepEqual(csp.resourceDomains,[]);
  const status=await client.callTool({name:"pua_setup",arguments:{action:"status"}});
  assert.equal(status.isError,false);
  assert.equal(data(status).service.ready,true);
  const tree=await client.callTool({name:"pua_observe",arguments:{mode:"tree"}});
  assert(!tree.isError);assert(JSON.stringify(tree).includes("Synthetic screen content"));
  for(const [name,args] of mutations){
   const reply=await client.callTool({name,arguments:args});
   assert.equal(reply.isError,true, name);
   assert.equal(data(reply).error.code,"safety_policy",name);
  }
 }finally{await client.close();}
}
console.log(JSON.stringify({connections:2,blockedMutations:mutations.length*2}));
'''.replace("CLIENT",json.dumps((SDK/"index.js").as_uri())).replace("STDIO",json.dumps((SDK/"stdio.js").as_uri())).replace("CONFIG",json.dumps(config))
            result=subprocess.run([shutil.which("node"),"--input-type=module"],input=program,text=True,
                                  capture_output=True,cwd=ROOT,env=dict(os.environ,IPHONE_USE_ANALYTICS="0",DO_NOT_TRACK="1"),timeout=20)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(json.loads(result.stdout),{"connections":2,"blockedMutations":16})
            self.assertTrue(any("/source" in path for _,path,_ in requests))
            self.assertTrue(all(method=="GET" or (method=="POST" and path.endswith("/appium/settings"))
                                for method,path,_ in requests),requests)


if __name__=="__main__":unittest.main()
