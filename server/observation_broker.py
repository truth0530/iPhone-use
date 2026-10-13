"""Private inherited channel: only observation requests can reach the fixed WDA endpoint."""
import json
import math
import os
from pathlib import Path
import socket
import struct
import sys
import threading
import time

REQUEST_LIMIT=65536
RESPONSE_LIMIT=32*1024*1024
_channel=None
_lock=threading.Lock()


def remaining(deadline):
    budget=deadline-time.monotonic()
    if budget<=0:raise socket.timeout("Observation broker deadline expired.")
    return budget


def receive(channel,limit,deadline=None):
    def exact(size):
        chunks=[]
        while size:
            if deadline is not None:channel.settimeout(remaining(deadline))
            chunk=channel.recv(size)
            if not chunk:raise EOFError("Observation broker channel closed.")
            chunks.append(chunk);size-=len(chunk)
        return b"".join(chunks)
    size=struct.unpack("!I",exact(4))[0]
    if not 0<size<=limit:raise ValueError("Observation broker frame exceeds its limit.")
    value=json.loads(exact(size))
    if deadline is not None:remaining(deadline)
    return value


def send(channel,value,limit,deadline=None):
    data=json.dumps(value,ensure_ascii=False,allow_nan=False,separators=(",",":")).encode()
    if len(data)>limit:raise ValueError("Observation broker frame exceeds its limit.")
    if deadline is not None:channel.settimeout(remaining(deadline))
    channel.sendall(struct.pack("!I",len(data))+data)


def request(method,path,payload,budget):
    """All clients in the MCP process share one serialized, non-reconnectable channel."""
    global _channel
    from wda_client import WDAError
    if type(budget) not in (int,float) or not math.isfinite(budget) or not 0<budget<=30:
        raise WDAError("safety_policy","Invalid observation broker time budget.",details={"action_executed":False})
    deadline=time.monotonic()+budget
    if not _lock.acquire(timeout=budget):
        raise WDAError("isolation_busy","Observation broker was busy for the entire request budget.",details={"action_executed":False})
    try:
        try:
            if _channel is None:
                fd=int(os.environ["IPHONE_USE_BROKER_FD"])
                _channel=socket.socket(fileno=os.dup(fd))
            send(_channel,{"method":method,"path":path,"payload":payload,"budget":remaining(deadline)},REQUEST_LIMIT,deadline)
            reply=receive(_channel,RESPONSE_LIMIT,deadline)
            if not isinstance(reply,dict) or set(reply) not in ({"result"},{"error"}):
                raise ValueError("Invalid observation broker reply.")
            if "result" in reply and not isinstance(reply["result"],dict):
                raise ValueError("Invalid observation broker result.")
            if "error" in reply:
                error=reply["error"]
                if (not isinstance(error,dict) or set(error)!={"code","message"}
                        or not isinstance(error["code"],str) or not isinstance(error["message"],str)):
                    raise ValueError("Invalid observation broker error.")
        except (OSError,EOFError,ValueError,KeyError) as error:
            if _channel is not None:_channel.close()
            # Never reconnect/fall back to HTTP after an ambiguous frame/timeout.
            raise WDAError("isolation_unavailable","Observation broker is unavailable; direct network fallback is forbidden.") from error
        if "error" in reply:
            error=reply["error"]
            raise WDAError(error["code"],error["message"],details={"action_executed":False})
        return reply["result"]
    finally:
        _lock.release()


def serve(channel,endpoint):
    from wda_client import WDAClient,WDAError
    # No environment-controlled destinations, DNS, redirects or proxy handlers.
    client=WDAClient(endpoint,read_only=True,tls_state=os.environ.get("IPHONE_USE_STATE_DIR"))
    try:
        while True:
            message=receive(channel,REQUEST_LIMIT)
            try:
                if not isinstance(message,dict) or set(message)!={"method","path","payload","budget"}:
                    raise ValueError("Invalid broker request fields.")
                method,path,payload,budget=(message[key] for key in ("method","path","payload","budget"))
                if (not isinstance(method,str) or not isinstance(path,str) or len(path)>1024
                        or type(budget) not in (int,float) or not math.isfinite(budget) or not 0<budget<=30
                        or not WDAClient.allowed_observation_request(method,path,payload)):
                    raise ValueError("Request is forbidden by the observation broker policy.")
                result=client.request(method,path,payload,timeout=budget)
                reply={"result":result}
            except ValueError:
                reply={"error":{"code":"safety_policy","message":"Request is forbidden by the observation broker policy."}}
            except WDAError as error:
                reply={"error":{"code":error.code,"message":str(error)}}
            send(channel,reply,RESPONSE_LIMIT)
    except (EOFError,OSError,ValueError):
        return 1
    finally:
        client.close();channel.close()


if __name__=="__main__":
    # The trusted launcher passes a private socketpair and a validated fixed URL.
    sys.path.insert(0,str(Path(__file__).resolve().parent))
    sys.exit(serve(socket.socket(fileno=int(sys.argv[1])),sys.argv[2]))
