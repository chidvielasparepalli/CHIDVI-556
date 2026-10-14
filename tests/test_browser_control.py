"""Semantic browser tests use mocked pages; no browser or network is opened."""
import asyncio
import concurrent.futures
import json
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import AsyncMock, Mock, patch

from actions import browser_control as browser
from core.desktop_control import DesktopError


def locator(count=1, name="Search"):
    item = Mock()
    item.count = AsyncMock(return_value=count)
    item.is_visible = AsyncMock(return_value=True)
    item.is_enabled = AsyncMock(return_value=True)
    item.evaluate = AsyncMock(return_value={"name": name, "secret": False, "type": "button"})
    item.bounding_box = AsyncMock(return_value={"x": 5, "y": 10, "width": 80, "height": 30})
    item.scroll_into_view_if_needed = AsyncMock()
    item.click = AsyncMock()
    item.fill = AsyncMock()
    return item


class BrowserTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        with patch.object(browser, "_resolve_browser", return_value={}):
            self.session = browser._BrowserSession("chrome")
        self.page = Mock(url="https://example.test/path?token=private#code")
        self.page.bring_to_front = AsyncMock()
        self.page.evaluate = AsyncMock(return_value=True)
        self.session._get_page = AsyncMock(return_value=self.page)
        self.state = {"url": "https://example.test/path", "controls": [], "sensitive": False, "challenge": False}
        self.session.inspect = AsyncMock(return_value=self.state)
        self.service = Mock()
        self.service.speak_click = Mock()
        self.patch = patch.object(browser, "get_service", return_value=self.service)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    async def test_unique_target_speech_ring_click_then_observe(self):
        target = locator()
        result = json.loads(await self.session._click_target(self.page, target))
        self.service.speak_click.assert_called_once()
        self.page.evaluate.assert_awaited_once_with(browser._CLICK_RING, target.bounding_box.return_value)
        target.click.assert_awaited_once()
        self.assertTrue(result["dispatched"])
        self.assertFalse(result["verified"])
        self.assertGreaterEqual(self.session.inspect.await_count, 3)

    async def test_ambiguous_target_never_clicks(self):
        target = locator(count=2)
        with self.assertRaisesRegex(DesktopError, "found 2"):
            await self.session._click_target(self.page, target)
        target.click.assert_not_awaited()

    async def test_changed_target_after_speech_is_refused(self):
        target = locator()
        target.evaluate.side_effect = [{"name": "Search"}, {"name": "Delete"}]
        with self.assertRaisesRegex(DesktopError, "changed"):
            await self.session._click_target(self.page, target)
        target.click.assert_not_awaited()

    async def test_challenge_and_credentials_pause(self):
        for flag in ("challenge", "sensitive"):
            target = locator()
            self.session.inspect.return_value = {**self.state, flag: True}
            with self.assertRaises(DesktopError):
                await self.session._click_target(self.page, target)
            target.click.assert_not_awaited()

    async def test_sensitive_requires_unforgeable_ui_confirmation(self):
        target = locator(name="Send")
        with patch.object(browser.confirm, "pending_title", return_value=""), patch.object(browser.confirm, "request", return_value="pending") as request:
            self.assertEqual(await self.session._click_target(self.page, target), "pending")
            request.assert_called_once()
        target.click.assert_not_awaited()

    async def test_approval_does_not_follow_same_named_target_to_different_page(self):
        target = locator(name="Send")
        with self.assertRaisesRegex(DesktopError, "changed"):
            await self.session._click_target(self.page, target, approved=True,
                expected={"target": target.evaluate.return_value, "url": "https://example.test/other"})
        target.click.assert_not_awaited()

    async def test_no_fallback_retry_after_click_failure(self):
        target = locator()
        target.click.side_effect = browser.PlaywrightTimeout("uncertain")
        self.page.get_by_role.side_effect = lambda role, **kwargs: target if role == "button" else locator(0)
        with self.assertRaises(browser.PlaywrightTimeout):
            await self.session.smart_click("Search")
        target.click.assert_awaited_once()
        self.page.get_by_text.assert_not_called()

    async def test_cross_role_ambiguity_is_refused(self):
        target = locator()
        self.page.get_by_role.side_effect = lambda role, **kwargs: target if role in ("button", "link") else locator(0)
        with self.assertRaisesRegex(DesktopError, "Multiple"):
            await self.session.smart_click("Search")
        target.click.assert_not_awaited()

    async def test_failed_speech_or_highlight_blocks_click(self):
        target = locator()
        self.service.speak_click.side_effect = DesktopError("speech unavailable")
        with self.assertRaises(DesktopError):
            await self.session._click_target(self.page, target)
        target.click.assert_not_awaited()
        self.service.speak_click.side_effect = None
        self.page.evaluate.return_value = False
        with self.assertRaises(DesktopError):
            await self.session._click_target(self.page, target)
        target.click.assert_not_awaited()

    async def test_modified_enter_cannot_bypass_submission_confirmation(self):
        self.page.keyboard = Mock(press=AsyncMock())
        for key in ("Control+Enter", "Shift+Enter", "Meta+Enter", "NumpadEnter"):
            result = await self.session.press(key)
            self.assertIn("named semantic click", result)
        self.page.keyboard.press.assert_not_awaited()

    async def test_target_move_during_ring_never_clicks(self):
        target = locator()
        target.bounding_box.side_effect = [dict(x=5, y=10, width=80, height=30), dict(x=10, y=15, width=80, height=30)]
        with self.assertRaisesRegex(DesktopError, "moved during highlighting"):
            await self.session._click_target(self.page, target)
        target.click.assert_not_awaited()

    async def test_inspect_url_omits_authentication_query(self):
        self.page.evaluate.return_value = {"controls": [], "sensitive": False}
        value = await browser._BrowserSession.inspect(self.session, self.page)
        self.assertEqual(value["url"], "https://example.test")
        self.assertNotIn("private", json.dumps(value))

    async def test_navigation_events_are_origin_only_and_sensitive_paths_pause(self):
        callback = Mock()
        with patch.object(browser, "_navigation_callback", callback):
            await self.session._publish_navigation(self.page)
            callback.assert_called_once_with("https://example.test", "Authentication")
            callback.reset_mock()
            self.page.evaluate.return_value = False
            await self.session._publish_navigation(self.page)
            callback.assert_not_called()

    async def test_navigation_failure_never_retries_or_claims_success(self):
        self.page.goto = AsyncMock(side_effect=browser.PlaywrightTimeout("private value"))
        result = await self.session.go_to("https://example.test/next")
        self.assertIn("unverified", result)
        self.assertNotIn("private", result)
        self.page.goto.assert_awaited_once()

    async def test_new_tab_navigation_cannot_overwrite_old_focused_page(self):
        new = Mock(url="https://example.test/new", goto=AsyncMock())
        self.page.context = Mock(new_page=AsyncMock(return_value=new))
        self.session.go_to = AsyncMock()
        await self.session.new_tab("https://example.test/new")
        new.goto.assert_awaited_once()
        self.session.go_to.assert_not_awaited()


class BrowserConnectionTests(unittest.TestCase):
    def test_actual_page_scripts_detect_credentials_from_placeholder_or_label(self):
        import playwright
        node = Path(playwright.__file__).parent / "driver" / ("node.exe" if os.name == "nt" else "node")
        if not node.exists():
            self.skipTest("Playwright's bundled JavaScript runtime is unavailable")
        program = r"""
const fs=require('fs'), scripts=JSON.parse(fs.readFileSync(0,'utf8'));
const fixtures=[{placeholder:'One-time code'}, {labels:[{textContent:'Verification code'}]}, {attrs:{'aria-label':'Security code'}}];
for(const fixture of fixtures){
 const e=Object.assign({type:'text',tagName:'INPUT',name:'',id:'',autocomplete:'',placeholder:'',labels:[],attrs:{},
  getAttribute(n){return this.attrs[n]||this[n]||null;},getBoundingClientRect(){return{x:0,y:0,width:10,height:10};}},fixture);
 Object.defineProperty(e,'value',{get(){throw Error('Credential value must never be read');}});
 global.document={title:'Test',querySelectorAll:s=>s==='iframe'?[]:[e],getElementById:()=>null,activeElement:null};
 global.location={hostname:'example.test'};global.innerWidth=100;global.innerHeight=100;global.devicePixelRatio=1;
 global.getComputedStyle=()=>({display:'block',visibility:'visible'});
 if(!(eval('('+scripts.target+')'))(e).secret)throw Error('Target missed credential field');
 if(!(eval('('+scripts.inspect+')'))().sensitive)throw Error('Page missed credential field');
}
"""
        kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        result = subprocess.run([str(node), "-e", program], input=json.dumps({"target": browser._TARGET_STATE, "inspect": browser._INSPECT_PAGE}), text=True, capture_output=True, timeout=5, **kwargs)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_cdp_only_loopback_without_credentials(self):
        for value in ("http://example.com:9222", "http://user:secret@localhost:9222", "file:///tmp/browser", "http://localhost"):
            with patch.dict(os.environ, {"CHIDVI_CHROME_CDP": value}), self.assertRaises(ValueError):
                browser._cdp_endpoint()
        with patch.dict(os.environ, {"CHIDVI_CHROME_CDP": "http://127.0.0.1:9222"}):
            self.assertEqual(browser._cdp_endpoint(), "http://127.0.0.1:9222")

    def test_timeout_cancels_pending_browser_future(self):
        session = browser._BrowserSession.__new__(browser._BrowserSession)
        session._loop = Mock()
        future = Mock()
        future.result.side_effect = concurrent.futures.TimeoutError()
        with patch.object(asyncio, "run_coroutine_threadsafe", return_value=future):
            with self.assertRaises(concurrent.futures.TimeoutError):
                session.run(Mock(), timeout=1)
        future.cancel.assert_called_once()

    def test_stop_cancels_inflight_browser_future_before_timeout(self):
        session = browser._BrowserSession.__new__(browser._BrowserSession)
        session._loop = Mock()
        future = Mock()
        future.result.side_effect = concurrent.futures.TimeoutError()
        future.done.return_value = False
        service = Mock()
        service.check_cancelled.side_effect = [None, DesktopError("cancelled")]
        with patch.object(asyncio, "run_coroutine_threadsafe", return_value=future), patch.object(browser, "get_service", return_value=service):
            with self.assertRaisesRegex(DesktopError, "cancelled"):
                session.run(Mock(), timeout=60)
        future.cancel.assert_called_once()
        future.result.assert_called_once()

    def test_untrusted_schemes_and_embedded_credentials_rejected(self):
        for url in ("file:///C:/secret", "https://user:password@example.test"):
            with self.assertRaises(DesktopError):
                browser._normalize_url(url)

    def test_errors_omit_playwright_details_and_input_values(self):
        self.assertEqual(browser._safe_error(ValueError("secret input")), "ValueError")


if __name__ == "__main__":
    unittest.main()
