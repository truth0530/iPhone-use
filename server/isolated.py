"""Fail-closed macOS launcher for the observation-only MCP server."""
import argparse
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
from urllib.parse import urlsplit

ROOT=Path(__file__).resolve().parent.parent


def quoted(value):
    value=str(value)
    if any(ord(char)<32 for char in value):raise ValueError("Control characters are forbidden in sandbox paths.")
    return json.dumps(value,ensure_ascii=False)


def python_executable():
    # macOS framework bin/python is a launcher which spawns this actual binary.
    framework=Path(sys.base_prefix)/"Resources/Python.app/Contents/MacOS/Python"
    return framework.resolve() if framework.is_file() else Path(sys.executable).resolve()


def sandbox_profile(state,port=None):
    executable=python_executable()
    reads=[ROOT/"server",ROOT/"assets",ROOT/"plugin.json",
           Path(sys.base_prefix).resolve(),Path("/System"),Path("/usr/lib"),Path("/usr/share"),
           Path("/private/var/db/dyld"),Path("/private/var/db/timezone")]
    # Homebrew's Python extension libraries link to separately installed formulae.
    if executable.is_relative_to(Path("/opt/homebrew/Cellar")):reads.append(Path("/opt/homebrew/Cellar"))
    if executable.is_relative_to(Path("/usr/local/Cellar")):reads.append(Path("/usr/local/Cellar"))
    paths=" ".join("(subpath "+quoted(path)+")" for path in reads)
    state_reads=""
    tls_reads=""
    if port is not None:
        tls_dir=Path(state)/"tls"
        tls_reads=" ".join("(literal "+quoted(tls_dir/name)+")" for name in ("ca.pem","client.pem","client.key"))
    writes='(allow file-write* (literal "/dev/null"))'
    if port is None:
        state_reads=" ".join("(literal "+quoted(state/name)+")" for name in
                             ("config.json","device.json","session.json","screen-state.json","operation.lock",".screen-state.lock",".lock"))
        state_reads+=" "+" ".join("(subpath "+quoted(state/name)+")" for name in ("artifacts","jobs","logs"))
        files=" ".join("(literal "+quoted(state/name)+")" for name in
                       ("operation.lock","session.json","screen-state.json",".screen-state.lock",".lock"))
        temporary=quoted("^"+re.escape(str(state))+r"/(session-[0-9]+\.tmp|\.screen-[-_A-Za-z0-9]+)$")
        writes+=f'\n(allow file-write* {files} (regex {temporary}) (subpath {quoted(state/"artifacts")}))'
    return f'''(version 1)
(deny default)
(import "dyld-support.sb")
(allow process-exec (literal {quoted(executable)}))
(allow file-read-metadata)
(allow file-read-data {paths} {state_reads} {tls_reads})
(allow file-map-executable {paths})
{writes}
(allow sysctl-read)
(allow signal (target self))
{('(allow network-outbound (remote ip "localhost:'+str(port)+'"))') if port is not None else ''}
'''


def launch_parameters(state=None,url=None):
    if sys.platform!="darwin" or not Path("/usr/bin/sandbox-exec").is_file():
        raise ValueError("Observation isolation requires macOS sandbox-exec; no unsandboxed fallback is allowed.")
    # Import only our own stdlib-based configuration validation, never user paths.
    sys.path.insert(0,str(ROOT/"server"))
    from wda_setup import state_directory,SetupManager
    state=state_directory(state).resolve()
    # Validate BEFORE creating state, including all enclosing Git checkouts.
    if (state==ROOT or ROOT in state.parents or state in ROOT.parents or state==Path.home()
            or state in Path.home().parents or any((parent/".git").exists() for parent in (state,*state.parents))):
        raise ValueError("Isolated runtime state must be outside every Git checkout.")
    if url is None:
        config=state/"config.json"
        configured=json.loads(config.read_text()).get("local_port",18100) if config.is_file() else 18100
        url=os.environ.get("WDA_URL") or "http://127.0.0.1:"+str(configured)
    parsed=urlsplit(url)
    # SetupManager also checks credentials, scheme, path, query and fragment.
    manager=SetupManager(state,url)
    # Writable observation files cannot alias trusted configuration/build files.
    for name in ("operation.lock","session.json","screen-state.json",".screen-state.lock",".lock",
                 "config.json","device.json","build.json","artifacts","jobs","logs"):
        path=state/name
        if path.is_symlink():raise ValueError("Observation state must not use symlinks.")
        candidates=[path]
        if path.is_dir():candidates.extend(path.rglob("*"))
        for candidate in candidates:
            if candidate.is_symlink() or (candidate.is_file() and candidate.stat().st_nlink!=1):
                raise ValueError("Observation state must not use symlinks or hard links.")
    # The server creates these names, and the seatbelt allows the writes. A leftover regular file is fine.
    writable_temp=re.compile(r"^(?:session-[0-9]+\.tmp|\.screen-[-_A-Za-z0-9]+)$")
    for child in sorted(state.iterdir(), key=lambda item:item.name):
        if not writable_temp.fullmatch(child.name):continue
        if child.is_symlink():raise ValueError("Observation state must not use symlinks.")
        if not child.is_file() or child.stat().st_nlink!=1:
            raise ValueError("Observation state must not use symlinks or hard links.")
    if not 1<=manager.port<=65535:raise ValueError("Invalid WDA port.")
    host="127.0.0.1" if parsed.hostname=="localhost" else parsed.hostname
    endpoint=f"http://{'['+host+']' if ':' in host else host}:{manager.port}"
    environment={"HOME":str(Path.home()),"PATH":"/usr/bin:/bin","LC_CTYPE":"UTF-8",
                 "IPHONE_USE_ANALYTICS":"0","DO_NOT_TRACK":"1","IPHONE_USE_SAFETY_MODE":"read-only",
                 "IPHONE_USE_STATE_DIR":str(state),"WDA_URL":endpoint}
    bootstrap="import sys;sys.path.insert(0,sys.argv.pop(1));import iphone_use;sys.exit(iphone_use.main())"
    command=["/usr/bin/sandbox-exec","-p",sandbox_profile(state),str(python_executable()),
             "-I","-S","-B","-c",bootstrap,str(ROOT/"server"),"--state-dir",str(state),"--url",endpoint]
    return command,environment


def run_isolated(command,environment):
    """Supervise two fixed sandboxed children; never interpret MCP/tool content here."""
    endpoint=environment["WDA_URL"]
    state=Path(environment["IPHONE_USE_STATE_DIR"])
    # Inherited connected sockets work without granting UNIX socket creation/connect.
    client_channel,broker_channel=socket.socketpair()
    broker_command=["/usr/bin/sandbox-exec","-p",sandbox_profile(state,urlsplit(endpoint).port),
                    str(python_executable()),"-I","-S","-B",str(ROOT/"server/observation_broker.py"),
                    str(broker_channel.fileno()),endpoint]
    children=[]
    def stop(signum,frame):
        for child in children:
            if child.poll() is None:child.terminate()
    previous={signum:signal.signal(signum,stop) for signum in (signal.SIGINT,signal.SIGTERM)}
    try:
        broker=subprocess.Popen(broker_command,env=environment,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,
                                pass_fds=(broker_channel.fileno(),))
        children.append(broker);broker_channel.close()
        mcp_environment=dict(environment,IPHONE_USE_BROKER_FD=str(client_channel.fileno()))
        server=subprocess.Popen(command,env=mcp_environment,pass_fds=(client_channel.fileno(),))
        children.append(server);client_channel.close()
        while True:
            try:return server.wait(timeout=0.1)
            except subprocess.TimeoutExpired:
                if broker.poll() is not None:
                    stop(None,None)
                    raise ValueError("Observation broker exited; no direct network fallback is allowed.")
    finally:
        client_channel.close();broker_channel.close()
        stop(None,None)
        for child in children:
            try:child.wait(timeout=3)
            except subprocess.TimeoutExpired:child.kill();child.wait()
        for signum,handler in previous.items():signal.signal(signum,handler)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--state-dir");parser.add_argument("--url")
    args=parser.parse_args()
    try:
        command,environment=launch_parameters(args.state_dir,args.url)
        return run_isolated(command,environment)
    except (OSError,ValueError) as error:
        print("iPhone Use isolation refused: "+str(error),file=sys.stderr)
        return 1


if __name__=="__main__":sys.exit(main())
