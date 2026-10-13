"""Adversarial private-channel tests; all traffic stays in socketpairs/loopback."""
import json
from pathlib import Path
import socket
import struct
import sys
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"server"))
import observation_broker as broker
from wda_client import WDAError
from test_client import fake_server


class BrokerBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.client,self.peer=socket.socketpair()
        self.addCleanup(self.client.close);self.addCleanup(self.peer.close)
        self.peer.settimeout(2)
        for mocked in (patch.object(broker,"_channel",self.client),patch.object(broker,"_lock",threading.Lock())):
            mocked.start();self.addCleanup(mocked.stop)

    def run_peer(self,action):
        def work():
            try:action()
            except (OSError,EOFError):pass
        thread=threading.Thread(target=work,daemon=True)
        thread.start()
        self.addCleanup(thread.join,2)
        return thread

    def test_waiting_for_busy_channel_respects_original_request_budget(self):
        broker._lock.acquire()
        release=threading.Timer(0.35,broker._lock.release)
        release.start();self.addCleanup(release.join,1)
        started=time.monotonic()
        with self.assertRaises(WDAError) as caught:broker.request("GET","/status",None,0.06)
        self.assertLess(time.monotonic()-started,0.25)
        self.assertEqual(caught.exception.code,"isolation_busy")
        self.peer.settimeout(0.02)
        with self.assertRaises(socket.timeout):self.peer.recv(1)

    def test_fragmented_reply_cannot_extend_total_deadline_and_channel_stays_closed(self):
        def slow_reply():
            broker.receive(self.peer,broker.REQUEST_LIMIT)
            data=json.dumps({"result":{"value":True}}).encode()
            self.peer.sendall(struct.pack("!I",len(data)))
            for index in range(0,len(data),3):
                time.sleep(0.03);self.peer.sendall(data[index:index+3])
        self.run_peer(slow_reply)
        started=time.monotonic()
        with self.assertRaises(WDAError) as caught:broker.request("GET","/status",None,0.08)
        self.assertEqual(caught.exception.code,"isolation_unavailable")
        self.assertLess(time.monotonic()-started,0.25)
        with self.assertRaises(WDAError) as second:broker.request("GET","/status",None,0.1)
        self.assertEqual(second.exception.code,"isolation_unavailable")

    def test_concurrent_calls_keep_each_reply_bound_to_its_request(self):
        with fake_server(lambda method,path,*args:(200,{"value":path},0,False)) as (url,requests,_):
            serving=threading.Thread(target=broker.serve,args=(self.peer,url),daemon=True);serving.start()
            results={};errors=[]
            def call(index):
                path=f"/session/sid-{index}/source?format=xml"
                try:results[index]=broker.request("GET",path,None,2)["value"]
                except Exception as error:errors.append(str(error))
            callers=[threading.Thread(target=call,args=(index,)) for index in range(12)]
            for thread in callers:thread.start()
            for thread in callers:thread.join(3)
            self.client.close();serving.join(2)
            self.assertFalse(errors,errors)
            self.assertEqual(results,{index:f"/session/sid-{index}/source?format=xml" for index in range(12)})
            self.assertEqual(len(requests),12)
            self.assertFalse(serving.is_alive())

    def test_eof_and_oversized_reply_fail_closed_without_reopening_channel(self):
        for reply in (b"",struct.pack("!I",broker.RESPONSE_LIMIT+1)):
            with self.subTest(reply=reply):
                client,peer=socket.socketpair()
                def respond():
                    broker.receive(peer,broker.REQUEST_LIMIT)
                    if reply:peer.sendall(reply)
                    peer.close()
                with client,peer,patch.object(broker,"_channel",client):
                    thread=threading.Thread(target=respond,daemon=True);thread.start()
                    with self.assertRaises(WDAError) as caught:broker.request("GET","/status",None,0.5)
                    self.assertEqual(caught.exception.code,"isolation_unavailable")
                    with self.assertRaises(WDAError):broker.request("GET","/status",None,0.5)
                    thread.join(1)

    def test_malformed_nested_reply_is_rejected_as_channel_failure(self):
        for reply in ({"result":"not an object"},{"error":None},{"error":{"code":4,"message":[]}}):
            with self.subTest(reply=reply):
                client,peer=socket.socketpair()
                def respond():
                    broker.receive(peer,broker.REQUEST_LIMIT);broker.send(peer,reply,broker.RESPONSE_LIMIT)
                with client,peer,patch.object(broker,"_channel",client):
                    thread=threading.Thread(target=respond,daemon=True);thread.start()
                    with self.assertRaises(WDAError) as caught:broker.request("GET","/status",None,0.5)
                    self.assertEqual(caught.exception.code,"isolation_unavailable")
                    thread.join(1)

    def test_oversized_request_frame_stops_broker_before_any_http_request(self):
        with fake_server(lambda *args:(200,{"value":True},0,False)) as (url,requests,_):
            serving=threading.Thread(target=broker.serve,args=(self.peer,url),daemon=True);serving.start()
            self.client.sendall(struct.pack("!I",broker.REQUEST_LIMIT+1))
            self.client.settimeout(1)
            self.assertEqual(self.client.recv(1),b"")
            serving.join(1)
            self.assertFalse(serving.is_alive());self.assertEqual(requests,[])


if __name__=="__main__":unittest.main()
