"""Exercise the actual form driver in headless Edge with all traffic intercepted."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

from playwright.async_api import BrowserType, async_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import browser_upload as upload


CHANNEL = "UC" + "a" * 22
OTHER_CHANNEL = "UC" + "b" * 22
VIDEO_ID = "aB12cDE_3fG"
TIKTOK_VIDEO = "7281234567890123456"
CONFIG = {"browser": "edge", "debug_address": ""}
CLIP = {"title": "Fixture clip", "caption": "Fixture caption\n#shorts"}
GOOGLE_SIGN_IN = "https://accounts.google.com/signin/v2/identifier"

YOUTUBE = """<!doctype html><meta charset=utf-8>
<script>history.replaceState(null, '', '/channel/__CHANNEL__');</script>
<a href="https://youtu.be/oldvideo111">An older unrelated upload</a>
<button id="create-icon">Создать</button>
<button id="upload-video-button" hidden>Загрузить видео</button>
<input id="other-file" type="file" accept="audio/*" hidden>
<ytcp-uploads-dialog hidden>
  <input id="yt-file" type="file" accept="video/mp4,.mp4" hidden>
  <input id="generic-file" type="file" hidden>
  <section id="details" hidden>
    <div id="title-textarea"><div id="textbox" contenteditable="true" role="textbox"></div></div>
    <div id="description-textarea"><div id="textbox" contenteditable="true" role="textbox"></div></div>
    <tp-yt-paper-radio-button name="VIDEO_MADE_FOR_KIDS_NOT_MFK" aria-checked="true">Not for kids</tp-yt-paper-radio-button>
    __DISCLOSURE__
    <button id="next-button">Далее</button>
  </section>
  <section id="visibility" hidden>
    <tp-yt-paper-radio-button role="radio" name="PUBLIC" aria-checked="false" checked>Public</tp-yt-paper-radio-button>
    <button id="publish" disabled>Опубликовать</button>
  </section>
</ytcp-uploads-dialog>
<script>
const byId = id => document.getElementById(id);
const publicOption = document.querySelector('[name=PUBLIC]');
byId('create-icon').onclick = () => byId('upload-video-button').hidden = false;
byId('upload-video-button').onclick = () => {
  document.querySelector('ytcp-uploads-dialog').hidden = false;
  if (__SWITCH__) history.pushState(null, '', '/channel/__OTHER__');
};
byId('generic-file').onchange = () => window.fixtureEvent({kind: 'wrong_input'});
byId('yt-file').onchange = async () => {
  await window.fixtureEvent({kind: 'attached', file: byId('yt-file').files[0].name});
  byId('details').hidden = false;
};
let next = 0, uploadDetails;
byId('next-button').onclick = () => {
  if (++next === 3) {
    uploadDetails = {
      title: document.querySelector('#title-textarea #textbox').innerText,
      caption: document.querySelector('#description-textarea #textbox').innerText,
    };
    if (byId('promotion')) uploadDetails.promotion = byId('promotion').checked;
    byId('details').hidden = true;
    byId('visibility').hidden = false;
  }
};
publicOption.onclick = () => setTimeout(() => {
  publicOption.setAttribute('aria-checked', 'true');
  byId('publish').disabled = false;
}, 150);
byId('publish').onclick = async () => {
  await window.fixtureEvent({kind: 'posted', public: publicOption.getAttribute('aria-checked'),
    ...uploadDetails, next});
  document.body.insertAdjacentHTML('beforeend', '<div role="dialog"><p>Видео опубликовано.</p>' +
    '<a href="https://www.youtube.com/shorts/__VIDEO__?feature=share">Открыть</a></div>');
};
</script>"""

TIKTOK_HOME = """<!doctype html><meta charset=utf-8>
<style>nav {width: 140px; height: 45px;} nav a {display:none} nav:hover a {display:inline}</style>
<a href="https://www.tiktok.com/@unrelated">Profile</a>
<nav data-e2e="profile-icon"><button>Account</button>
<a data-e2e="profile-link" href="https://www.tiktok.com/__ACCOUNT__">View profile</a></nav>"""

TIKTOK_FRAME = """<!doctype html><meta charset=utf-8>
<input id="tt-file" type="file" accept="video/mp4,.mp4" hidden>
<input id="generic-file" type="file" hidden>
<main id="editor" hidden>
  <div contenteditable="true" role="textbox" aria-label="Unrelated editor"></div>
  <div id="caption" data-e2e="caption_editor" contenteditable="true"></div>
  __VISIBILITY__
  __DISCLOSURE__
  <button id="post" data-e2e="post_video_button">Post</button>
</main>
<script>
const byId = id => document.getElementById(id);
const currentVisibility = () => byId('visibility').tagName === 'SELECT' ?
  byId('visibility').selectedOptions[0].label : byId('selection').innerText;
byId('generic-file').onchange = () => window.fixtureEvent({kind: 'wrong_input'});
byId('tt-file').onchange = async () => {
  await window.fixtureEvent({kind: 'attached', file: byId('tt-file').files[0].name});
  byId('editor').hidden = false;
};
if (byId('visibility').tagName !== 'SELECT') {
  byId('visibility').onclick = () => byId('options').hidden = false;
  byId('everyone').onclick = () => setTimeout(() => {
    byId('selection').innerText = 'Everyone';
    byId('options').hidden = true;
  }, 150);
}
byId('post').onclick = async () => {
  await window.fixtureEvent({kind: 'post_click', public: currentVisibility()});
  document.body.insertAdjacentHTML('beforeend', '<div role="status">Your video has been uploaded.</div>');
  if (__ACK_ONLY__) return;
  document.body.insertAdjacentHTML('beforeend', '<div role="dialog" id="confirm"><button>Post now</button></div>');
  byId('confirm').querySelector('button').onclick = async () => {
    const result = {kind: 'posted', caption: byId('caption').innerText, public: currentVisibility()};
    if (byId('commercial')) {
      result.commercial = byId('commercial').getAttribute('aria-checked');
      result.disclosure = document.querySelector('input[name=brand-type]:checked')?.value;
    }
    await window.fixtureEvent(result);
    byId('confirm').remove();
    document.body.insertAdjacentHTML('beforeend', '<div role="dialog"><p>Your video has been published.</p>' +
      '<a href="https://www.tiktok.com/@artist/video/__VIDEO__">Watch your video</a></div>');
  };
};
</script>"""

CUSTOM_VISIBILITY = """<div id="visibility" role="combobox" class="tiktok-select-selector" tabindex="0">
<span id="selection" class="tiktok-select-selection-item">Only you</span></div>
<div id="options" role="listbox" hidden><button id="everyone" role="option">Everyone</button></div>"""
NATIVE_VISIBILITY = """<select id="visibility" aria-label="Who can watch this video">
<option value="private" selected>Only you</option><option value="public">Everyone</option></select>"""
YOUTUBE_DISCLOSURE = """<button onclick="document.getElementById('promotion-options').hidden=false">Show more</button>
<section id="promotion-options" hidden>
<label><input id="promotion" type="checkbox">My video contains paid promotion</label>
</section>"""
TIKTOK_DISCLOSURE = """<button onclick="document.getElementById('disclosure-options').hidden=false">Show more</button>
<section id="disclosure-options" hidden>
<button id="commercial" role="switch" aria-checked="false"
    onclick="this.setAttribute('aria-checked', 'true'); document.getElementById('brand-types').hidden=false">
    Disclose post content</button>
<div id="brand-types" hidden>
  <label><input type="radio" name="brand-type" value="own">Your brand</label>
  <label><input type="radio" name="brand-type" value="sponsored">Branded content</label>
</div>
</section>"""


class BrowserFormTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix="browser-forms-")
        self.addCleanup(self.scratch.cleanup)
        self.path = Path(self.scratch.name) / "fixture.mp4"
        self.path.write_bytes(b"offline form fixture")
        self.events, self.stages, self.unexpected = [], [], []
        self.tiktok_account = "@artist"
        self.switch_at_attachment = False
        self.switch_on_upload_route = False
        self.youtube_switch = self.ack_only = self.native_visibility = self.break_navigation = False
        self.login_required = False
        self.youtube_sign_in = False
        self.native_disclosure = False
        self.playwright = await async_playwright().start()
        self.addAsyncCleanup(self.playwright.stop)
        self.browser = await self.playwright.chromium.launch(channel="msedge", headless=True,
                                                              args=["--disable-background-networking"])
        self.addAsyncCleanup(self.browser.close)
        self.context = await self.browser.new_context(service_workers="block")
        self.addAsyncCleanup(self.context.close)
        await self.context.route("**/*", self.route)
        await self.context.expose_binding("fixtureEvent", self.record)
        self.session = upload.BrowserSession(self.scratch.name)
        self.session.context = self.context
        self.session.signature = ("edge", "")

    async def record(self, source, event):
        self.events.append(event)
        if event["kind"] == "attached" and self.switch_at_attachment:
            self.tiktok_account = "@other"

    async def route(self, route):
        url = urlsplit(route.request.url)
        if url.hostname == "studio.youtube.com":
            if self.break_navigation:
                await route.abort("timedout")
                return
            if self.youtube_sign_in:
                await route.fulfill(content_type="text/html", body=f'<script>location.replace("{GOOGLE_SIGN_IN}")</script>')
                return
            if url.path in {"/", f"/channel/{CHANNEL}"}:
                body = YOUTUBE.replace("__VIDEO__", VIDEO_ID).replace("__OTHER__", OTHER_CHANNEL)
                body = body.replace("__CHANNEL__", CHANNEL).replace("__SWITCH__", "true" if self.youtube_switch else "false")
                body = body.replace("__DISCLOSURE__", YOUTUBE_DISCLOSURE if self.native_disclosure else "")
                await route.fulfill(content_type="text/html; charset=utf-8", body=body)
                return
        if route.request.url == GOOGLE_SIGN_IN:
            await route.fulfill(content_type="text/html", body='<h1>Sign in</h1><input type="email" aria-label="Email">')
            return
        if url.hostname == "www.tiktok.com":
            if url.path == "/" and self.login_required:
                await route.fulfill(status=302, headers={"location": "https://www.tiktok.com/login"})
                return
            if url.path == "/login":
                await route.fulfill(content_type="text/html", body="<h1>Sign in</h1>")
                return
            if url.path == "/":
                await route.fulfill(content_type="text/html; charset=utf-8",
                                    body=TIKTOK_HOME.replace("__ACCOUNT__", self.tiktok_account))
                return
            if url.path == "/tiktokstudio/upload":
                if self.switch_on_upload_route:
                    self.tiktok_account = "@other"
                await route.fulfill(content_type="text/html", body='''<input type="file" accept="audio/*" hidden>
                    <iframe style="width:760px;height:500px" src="https://studio.tiktok.com/fixture-upload"></iframe>''')
                return
        if url.hostname == "studio.tiktok.com" and url.path == "/fixture-upload":
            body = TIKTOK_FRAME.replace("__VISIBILITY__", NATIVE_VISIBILITY if self.native_visibility else CUSTOM_VISIBILITY)
            body = body.replace("__VIDEO__", TIKTOK_VIDEO).replace("__ACK_ONLY__", "true" if self.ack_only else "false")
            body = body.replace("__DISCLOSURE__", TIKTOK_DISCLOSURE if self.native_disclosure else "")
            await route.fulfill(content_type="text/html; charset=utf-8", body=body)
            return
        self.unexpected.append(route.request.url)
        await route.abort()

    async def publish(self, platform, account, **fields):
        async def checkpoint(stage):
            self.stages.append(stage)
        return await self.session.publish(platform, CONFIG, self.path, {**CLIP, **fields}, account, checkpoint)

    async def test_youtube_uploads_video_to_bound_channel_and_confirms_post(self):
        self.assertEqual(await self.session.account("youtube", CONFIG), CHANNEL)
        self.assertEqual(await self.publish("youtube", CHANNEL), f"https://youtu.be/{VIDEO_ID}")
        self.assertEqual(self.stages, ["uploading", "publishing"])
        self.assertEqual(self.events, [{"kind": "attached", "file": "fixture.mp4"},
                                       {"kind": "posted", "public": "true", **CLIP, "next": 3}])
        self.assertEqual(self.unexpected, [])

    async def test_youtube_channel_change_blocks_before_file_attachment(self):
        self.youtube_switch = True
        with self.assertRaisesRegex(upload.PostingBlocked, "Канал в Studio изменился"):
            await self.publish("youtube", CHANNEL)
        self.assertEqual((self.stages, self.events), ([], []))

    async def test_youtube_sponsored_disclosure_is_checked_before_publish(self):
        self.native_disclosure = True
        url = await self.publish("youtube", CHANNEL, ad_disclosure="sponsored")
        self.assertEqual(url, f"https://youtu.be/{VIDEO_ID}")
        self.assertIs(self.events[-1]["promotion"], True)
        self.assertEqual(self.stages, ["uploading", "publishing"])

    async def test_tiktok_disclosure_uses_only_the_configured_brand_type(self):
        self.native_disclosure = True
        for disclosure in ("own", "sponsored"):
            with self.subTest(disclosure=disclosure):
                url = await self.publish("tiktok", "@artist", ad_disclosure=disclosure)
                self.assertEqual(url, f"https://www.tiktok.com/@artist/video/{TIKTOK_VIDEO}")
                self.assertEqual(self.events[-1]["commercial"], "true")
                self.assertEqual(self.events[-1]["disclosure"], disclosure)

    async def test_missing_native_disclosure_never_clicks_publish(self):
        original_wait = upload._wait

        async def short_disclosure_wait(probe, message, *, seconds=20):
            if message == upload.DISCLOSURE_UNAVAILABLE:
                seconds = 0.6
            return await original_wait(probe, message, seconds=seconds)

        with patch.object(upload, "_wait", short_disclosure_wait):
            with self.assertRaisesRegex(upload.PostingBlocked, "не подтвердила отметку"):
                await self.publish("youtube", CHANNEL, ad_disclosure="sponsored")
        self.assertEqual(self.stages, ["uploading"])
        self.assertEqual([event["kind"] for event in self.events], ["attached"])

    async def test_tiktok_chooses_trusted_iframe_and_clicks_confirmation_after_upload_ack(self):
        self.assertEqual(await self.session.account("tiktok", CONFIG), "@artist")
        self.assertEqual(await self.publish("tiktok", "@artist"), f"https://www.tiktok.com/@artist/video/{TIKTOK_VIDEO}")
        self.assertEqual(self.stages, ["uploading", "publishing", "publishing"])
        self.assertEqual(self.events, [{"kind": "attached", "file": "fixture.mp4"},
                                       {"kind": "post_click", "public": "Everyone"},
                                       {"kind": "posted", "public": "Everyone", "caption": CLIP["caption"]}])
        self.assertEqual(self.unexpected, [])

    async def test_tiktok_native_visibility_select_changes_actual_selected_option(self):
        self.native_visibility = True
        await self.publish("tiktok", "@artist")
        self.assertEqual(self.events[-1]["public"], "Everyone")

    async def test_tiktok_upload_acknowledgement_alone_is_not_publication(self):
        self.ack_only = True
        original_wait = upload._wait

        async def short_confirmation_wait(probe, message, *, seconds=20):
            if message.startswith("TikTok не показал подтверждение"):
                seconds = 0.7
            return await original_wait(probe, message, seconds=seconds)

        with patch.object(upload, "_wait", short_confirmation_wait):
            with self.assertRaisesRegex(upload.PostingBlocked, "не показал подтверждение"):
                await self.publish("tiktok", "@artist")
        self.assertEqual([event["kind"] for event in self.events], ["attached", "post_click"])

    async def test_tiktok_account_changes_are_blocked_before_attachment(self):
        self.switch_on_upload_route = True
        with self.assertRaisesRegex(upload.PostingBlocked, "аккаунт TikTok изменился"):
            await self.publish("tiktok", "@artist")
        self.assertEqual((self.stages, self.events), ([], []))

    async def test_tiktok_account_changes_after_attachment_never_click_post(self):
        self.switch_at_attachment = True
        with self.assertRaisesRegex(upload.PostingBlocked, "аккаунт TikTok изменился"):
            await self.publish("tiktok", "@artist")
        self.assertEqual(self.stages, ["uploading"])
        self.assertEqual([event["kind"] for event in self.events], ["attached"])

    async def test_navigation_errors_keep_manual_login_tab_open_and_retry_in_place(self):
        self.break_navigation = True
        with self.assertRaisesRegex(upload.PostingBlocked, "Вкладка входа осталась открыта"):
            await self.session.open("youtube", CONFIG)
        login = self.session.login_pages["youtube"]
        self.assertFalse(login.is_closed())
        with self.assertRaisesRegex(upload.PostingBlocked, "Не удалось открыть страницу площадки"):
            await self.session.account("youtube", CONFIG)
        self.assertFalse(login.is_closed())
        self.assertEqual(self.context.pages, [login])
        self.break_navigation = False
        await self.session.open("youtube", CONFIG)
        self.assertIs(self.session.login_pages["youtube"], login)
        await login.wait_for_url(f"https://studio.youtube.com/channel/{CHANNEL}", timeout=5000)

    async def test_youtube_manual_login_tab_survives_google_redirect_and_bind_probe(self):
        self.youtube_sign_in = True
        await self.session.open("youtube", CONFIG)
        login = self.session.login_pages["youtube"]
        await login.wait_for_url(GOOGLE_SIGN_IN, timeout=5000)
        self.assertTrue(await login.get_by_role("textbox", name="Email").is_visible())
        with self.assertRaises(upload.PostingBlocked):
            await self.session.account("youtube", CONFIG)
        self.assertFalse(login.is_closed())
        await self.session.open("youtube", CONFIG)
        self.assertIs(self.session.login_pages["youtube"], login)
        self.assertEqual(login.url, GOOGLE_SIGN_IN)
        self.assertEqual(self.context.pages, [login])
        self.assertEqual(self.unexpected, [])

    async def test_octo_open_uses_its_own_tab_and_leaves_other_tabs_untouched(self):
        existing = await self.context.new_page()
        config = {"browser": "octo", "debug_address": "http://127.0.0.1:9222"}
        self.session.signature = (config["browser"], config["debug_address"])
        await self.session.open("youtube", config)
        self.assertIsNot(self.session.login_pages["youtube"], existing)
        self.assertEqual(existing.url, "about:blank")
        self.assertFalse(existing.is_closed())

    async def test_login_redirect_prevents_binding_an_account(self):
        self.login_required = True
        with self.assertRaisesRegex(upload.PostingBlocked, "просит войти"):
            await self.session.account("tiktok", CONFIG)
        await self.session.open("tiktok", CONFIG)
        self.assertEqual(self.session.login_pages["tiktok"].url, upload.URLS["tiktok"])


class PersistentLoginTests(unittest.IsolatedAsyncioTestCase):
    async def test_fresh_edge_profile_keeps_one_login_tab_and_can_reopen(self):
        scratch = tempfile.TemporaryDirectory(prefix="persistent-login-")
        self.addCleanup(scratch.cleanup)
        session = upload.BrowserSession(scratch.name)
        self.addAsyncCleanup(session.close)
        launch = BrowserType.launch_persistent_context
        startups = []

        async def fixture_route(route):
            if route.request.url == upload.URLS["youtube"]:
                await route.fulfill(content_type="text/html", body=f'<script>location.replace("{GOOGLE_SIGN_IN}")</script>')
            elif route.request.url == GOOGLE_SIGN_IN:
                await route.fulfill(content_type="text/html", body='<h1>Sign in</h1>')
            else:
                await route.abort()

        async def launch_isolated_edge(browser_type, directory, **options):
            options.update(headless=True, service_workers="block", args=["--disable-background-networking"])
            context = await launch(browser_type, directory, **options)
            startups.append(list(context.pages))
            await context.route("**/*", fixture_route)
            return context

        with patch.object(BrowserType, "launch_persistent_context", launch_isolated_edge):
            await session.open("youtube", CONFIG)
            context = session.context
            login = session.login_pages["youtube"]
            await login.wait_for_url(GOOGLE_SIGN_IN, timeout=5000)
            if startups[0]:
                self.assertIs(login, startups[0][0])
            await session.open("youtube", CONFIG)
            self.assertEqual(context.pages, [login])
            await context.close()
            await session.open("youtube", CONFIG)
            reopened = session.login_pages["youtube"]
            await reopened.wait_for_url(GOOGLE_SIGN_IN, timeout=5000)
            self.assertIsNot(session.context, context)
            self.assertFalse(reopened.is_closed())


if __name__ == "__main__":
    unittest.main()
