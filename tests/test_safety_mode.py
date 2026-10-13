"""Offline policy and input-binding regressions; all device transports are fakes."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, PropertyMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from iphone_use import Runtime
from wda_client import WDAError
from wda_controller import PhoneController
from test_controller import FakeWDA


class SafetyModeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    def runtime(self, mode=None):
        with patch.dict(os.environ, {"IPHONE_USE_SAFETY_MODE": mode or "read-only"}):
            runtime = Runtime(self.directory.name)
        self.addCleanup(runtime.close)
        client = FakeWDA()
        client.close = Mock()
        runtime.client = client
        runtime.phone = PhoneController(client, self.directory.name, read_only=runtime.safety_mode!="standard")
        runtime.identify_device = Mock()
        runtime.screen.begin = Mock()
        return runtime, client

    def test_default_mode_blocks_mutations_before_any_device_or_setup_call(self):
        with patch.dict(os.environ, {}, clear=True):
            runtime = Runtime(self.directory.name)
        self.addCleanup(runtime.close)
        self.assertEqual(runtime.safety_mode, "read-only")
        runtime.client.request = Mock()
        runtime.identify_device = Mock()
        runtime.setup_manager.setup = Mock()
        runtime.screen.begin = Mock()
        cases = [("tap", {"x": 1, "y": 1}), ("swipe", {}),
                 ("type_text", {"text": "private input"}), ("press_button", {"name": "home"}),
                 ("launch_app", {"bundle_id": "com.example.app"}), ("collect_list", {}),
                 ("scroll_find", {"selector": {"label": "Target"}}), ("doctor", {}),
                 ("screen_action", {"action": "home"}), ("screen_action", {"action": "screenshot"})]
        cases += [("setup", {"action": action}) for action in ("fetch", "configure", "build", "start", "stop", "discover")]
        cases += [("apps", {"query": "test", "source": source}) for source in ("apple", "installed", "auto")]
        for op, args in cases:
            with self.subTest(op=op, args=args), self.assertRaises(WDAError) as caught:
                runtime.call("pua_" + op, args)
            self.assertEqual(caught.exception.code, "safety_policy")
            self.assertFalse(caught.exception.details["action_executed"])
            if op in ("setup", "tap"):
                self.assertIn("IPHONE_USE_SAFETY_MODE=standard", str(caught.exception))
                self.assertIn("yourself", str(caught.exception))
        for method in (runtime.client.request, runtime.identify_device, runtime.setup_manager.setup, runtime.screen.begin):
            method.assert_not_called()

    def test_batch_is_preflighted_before_first_step(self):
        for mode in ("read-only", "strict"):
            runtime, client = self.runtime(mode)
            runtime.phone.observe = Mock()
            forbidden = {"op": "press_button", "args": {"name": "home"}} if mode == "read-only" else {
                "op": "type_text", "args": {"text": "send", "submit": True}}
            with self.assertRaises(WDAError) as caught:
                runtime.call("pua_batch", {"steps": [{"op": "observe", "args": {}}, forbidden]})
            self.assertEqual(caught.exception.code, "safety_policy")
            runtime.phone.observe.assert_not_called()
            self.assertEqual(client.calls, [])

    def test_read_only_allows_local_reads_and_never_enables_recovery(self):
        runtime, client = self.runtime()
        runtime.setup_manager.pending_recovery = Mock(return_value=None)
        runtime.setup_manager.recover = Mock()
        runtime.ready_once = Mock(side_effect=WDAError("pua_foreground_unavailable", "fake fault"))
        result = runtime.call("pua_ready", {"recover": True, "screenshot": False})
        self.assertFalse(result["ready"])
        runtime.setup_manager.recover.assert_not_called()
        runtime.apps.lookup = Mock(return_value={"ok": True})
        self.assertTrue(runtime.call("pua_apps", {"query": "Settings"})["ok"])
        runtime.apps.lookup.assert_called_once_with(query="Settings", source="catalog")
        runtime.setup_manager.setup = Mock(return_value={"ok": True})
        self.assertTrue(runtime.call("pua_setup", {"action": "status"})["ok"])
        runtime.setup_manager.setup.assert_called_once_with(action="status")
        self.assertEqual(client.actions(), [])

    def test_strict_blocks_even_verified_mutations_before_controller_calls(self):
        runtime, client = self.runtime("strict")
        for op, args in (("press_button", {"name": "home", "verify": True, "observe": "both"}),
                         ("launch_app", {"bundle_id": "com.example.app", "verify": True}),
                         ("swipe", {"verify": True}), ("type_text", {"text": "draft", "verify": True}),
                         ("tap", {"selector": {"label": "Send"}, "expect": {"label": "Send"}})):
            method = Mock(return_value={"verified": True})
            setattr(runtime.phone, op, method)
            with self.assertRaises(WDAError) as caught:runtime.call("pua_" + op, args)
            self.assertEqual(caught.exception.code,"safety_policy")
            method.assert_not_called()
        self.assertEqual(client.actions(), [])

    def test_strict_blocks_submission_unchecked_taps_and_setup_bypasses(self):
        runtime, client = self.runtime("strict")
        for op, args in (("type_text", {"text": "send", "submit": True}),
                         ("type_text", {"text": "line\nbreak", "allow_newlines": True}),
                         ("tap", {"x": 1, "y": 1}), ("setup", {"action": "start"})):
            with self.assertRaises(WDAError) as caught:
                runtime.call("pua_" + op, args)
            self.assertEqual(caught.exception.code, "safety_policy")
        with self.assertRaises(WDAError):
            runtime.screen_action("home")
        self.assertEqual(client.calls, [])
        with self.assertRaises(WDAError):runtime.policy_arguments("tap", {"x": 1, "y": 1, "expect": {"label": "Next"}})
        with self.assertRaises(WDAError):runtime.policy_arguments("type_text", {"continue_token": "token"})

    def test_invalid_safety_mode_fails_closed(self):
        for mode in ("typo", ""):
            with self.subTest(mode=mode), self.assertRaises(WDAError) as caught:
                Runtime(self.directory.name, safety_mode=mode)
            self.assertEqual(caught.exception.code, "invalid_configuration")

    def test_legacy_continuations_through_runtime_keep_original_binding(self):
        for change in (None, "app", "shared_session"):
            with self.subTest(change=change):
                runtime, client = self.runtime("standard")
                client.focused = client.elements[0]
                runtime.phone.typing_piece = 2
                runtime.phone.call_budget = 0
                result = runtime.call("pua_type_text", {"text": "ABCD", "replace": False, "verify": True,"observe":"both"})
                self.assertFalse(result["input_complete"])
                if change == "app":client.app = "com.example.other"
                if change == "shared_session":
                    (runtime.state_dir / "session.json").write_text(json.dumps({"url": runtime.base_url, "session_id": "external-session"}))
                before = len(client.actions())
                if change:
                    with self.assertRaises(WDAError) as caught:
                        runtime.call("pua_type_text", {"continue_token": result["continue_token"]})
                    self.assertEqual(caught.exception.code, "input_target_changed")
                    self.assertEqual(len(client.actions()), before)
                    self.assertEqual(client.elements[0]["value"], "AB")
                else:
                    final = runtime.call("pua_type_text", {"continue_token": result["continue_token"]})
                    self.assertTrue(final["exact_readback"])
                    self.assertIn("image", final["observation"])
                    self.assertEqual(client.elements[0]["value"], "ABCD")

    def test_safe_controller_rejects_direct_writes_and_batch_before_reads(self):
        for mode in ("read-only","strict"):
            runtime,client=self.runtime(mode)
            for call in (lambda:runtime.phone.type_text(text="synthetic"),
                         lambda:runtime.phone.tap(selector={"label":"Send"},expect={"label":"Send"}),
                         lambda:runtime.phone.post("/wda/keys",{"value":["synthetic"]}),
                         lambda:runtime.phone.batch([{"op":"observe"},{"op":"press_button","args":{"name":"home"}}])):
                with self.assertRaises(WDAError) as caught:call()
                self.assertEqual(caught.exception.code,"safety_policy")
                self.assertFalse(caught.exception.details["action_executed"])
            self.assertEqual(client.calls,[])

    def test_raw_predicate_queries_and_nested_batch_are_blocked_before_device_reads(self):
        for mode in ("read-only","strict"):
            runtime,client=self.runtime(mode)
            selector={"predicate":"FUNCTION(SELF, 'description') != nil"}
            for op,args in (("find",{"selector":selector}),("wait",{"selector":selector}),
                            ("scroll_find",{"selector":selector,"max_swipes":0}),
                            ("batch",{"steps":[{"op":"observe","args":{}},{"op":"wait","args":{"selector":selector}}]})):
                with self.assertRaises(WDAError) as caught:runtime.call("pua_"+op,args)
                self.assertEqual(caught.exception.code,"safety_policy")
            for call in (lambda:runtime.phone.query(selector),lambda:runtime.phone.wait(selector)):
                with self.assertRaises(WDAError) as caught:call()
                self.assertEqual(caught.exception.code,"safety_policy")
            self.assertEqual(client.calls,[])

    def test_safe_modes_cannot_resume_a_legacy_typing_plan(self):
        for mode in ("read-only","strict"):
            runtime,client=self.runtime(mode)
            client.focused=client.elements[0]
            legacy=PhoneController(client,self.directory.name)
            legacy.typing_piece=2;legacy.call_budget=0
            first=legacy.type_text(text="ABCD",replace=False)
            runtime.phone.pending_input=legacy.pending_input
            before=list(client.calls)
            for call in (lambda:runtime.call("pua_type_text",{"continue_token":first["continue_token"]}),
                         lambda:runtime.phone.type_text(continue_token=first["continue_token"])):
                with self.assertRaises(WDAError) as caught:call()
                self.assertEqual(caught.exception.code,"safety_policy")
            self.assertEqual(client.calls,before)
            self.assertEqual(client.elements[0]["value"],"AB")

    def test_safety_modes_do_not_discover_devices_for_preview_labels(self):
        for mode in ("read-only", "strict"):
            runtime, _ = self.runtime(mode)
            # Restore the real helper hidden by the transport fixture.
            runtime.identify_device = Runtime.identify_device.__get__(runtime)
            runtime.setup_manager.discover = Mock()
            with patch.object(type(runtime.setup_manager), "config", new_callable=PropertyMock,
                              return_value={"udid": "fake-device"}):
                runtime.identify_device()
                runtime._device_lookup.join(timeout=2)
            runtime.setup_manager.discover.assert_not_called()

    def test_safety_modes_disable_analytics_even_if_environment_requests_it(self):
        for mode in ("read-only", "strict"):
            with patch("analytics.opted_out", return_value=False), patch("analytics.project_token", return_value="fake-token"):
                runtime, _ = self.runtime(mode)
                runtime.analytics.start()
            self.assertIsNone(runtime.analytics.worker)
            self.assertIsNone(runtime.analytics.ident)


class InputBindingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    def phone(self, verify=False):
        client = FakeWDA()
        client.focused = client.elements[0]
        phone = PhoneController(client, self.directory.name)
        phone.typing_piece = 2
        phone.call_budget = 0
        first = phone.type_text(text="ABCDEF", replace=False, verify=verify)
        self.assertEqual(first["characters"], 2)
        return phone, client, first["continue_token"]

    def test_changed_app_session_focus_or_secure_field_stops_before_remaining_keys(self):
        for verify in (False, True):
            for change in ("app", "session", "focus", "secure", "missing", "unreadable", "recreated_session"):
                with self.subTest(verify=verify, change=change):
                    phone, client, token = self.phone(verify)
                    original = client.elements[0]
                    other = copy.deepcopy(original)
                    other.update(id="other", value="", kind="XCUIElementTypeSecureTextField" if change == "secure" else "XCUIElementTypeTextField")
                    client.elements.append(other)
                    if change == "app":client.app = "com.example.other"
                    elif change == "session":client.session_id = "another-session"
                    elif change in ("focus", "secure"):client.focused = other
                    elif change == "missing":client.focused = None
                    original_session = client.session
                    def session(method, path, payload=None, timeout=None):
                        if path == "/element/active":
                            if change == "unreadable":raise WDAError("unknown error", "fake focus read failure")
                            if change == "recreated_session":client.session_id = "new-session"
                        return original_session(method, path, payload, timeout)
                    before = len(client.actions())
                    with patch.object(client, "session", side_effect=session), self.assertRaises(WDAError) as caught:
                        phone.type_text(continue_token=token)
                    self.assertEqual(caught.exception.code, "input_target_changed")
                    self.assertFalse(caught.exception.details["action_executed"])
                    self.assertEqual(caught.exception.details["characters_confirmed"], 2)
                    self.assertEqual(len(client.actions()), before)
                    self.assertEqual((original["value"], other["value"]), ("AB", ""))
                    self.assertIsNone(phone.pending_input)
                    with self.assertRaises(WDAError) as expired:
                        phone.type_text(continue_token=token)
                    self.assertEqual(expired.exception.code, "input_continuation_expired")

    def test_unchanged_target_continues_without_duplicate_characters(self):
        phone, client, token = self.phone(True)
        second = phone.type_text(continue_token=token)
        third = phone.type_text(continue_token=second["continue_token"])
        self.assertTrue(third["exact_readback"])
        self.assertEqual(client.elements[0]["value"], "ABCDEF")
        self.assertIsNone(phone.pending_input)

    def test_verified_input_rejects_external_edits_in_the_same_field(self):
        phone, client, token = self.phone(True)
        client.elements[0]["value"] = "user edit"
        before = len(client.actions())
        with self.assertRaises(WDAError) as caught:
            phone.type_text(continue_token=token)
        self.assertEqual(caught.exception.code, "input_target_changed")
        self.assertEqual(client.elements[0]["value"], "user edit")
        self.assertEqual(len(client.actions()), before)

    def test_focus_change_between_chunks_in_one_call_also_stops(self):
        phone, client, token = self.phone()
        phone.call_budget = 100
        original_session = client.session
        def session(method, path, payload=None, timeout=None):
            result = original_session(method, path, payload, timeout)
            if method == "POST" and path == "/wda/keys":client.app = "com.example.other"
            return result
        with patch.object(client, "session", side_effect=session), self.assertRaises(WDAError) as caught:
            phone.type_text(continue_token=token)
        self.assertTrue(caught.exception.details["action_executed"])
        self.assertEqual(caught.exception.details["characters_confirmed"], 4)
        self.assertEqual(client.elements[0]["value"], "ABCD")
        self.assertIsNone(phone.pending_input)
