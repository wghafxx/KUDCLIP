"""Publish daily releases through the visible browser's upload forms."""
import asyncio
from contextlib import suppress
import os
from pathlib import Path
import re
import time
from urllib.parse import parse_qs, urljoin, urlsplit

try:
    from playwright.async_api import Error as PlaywrightError
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError
except ImportError:
    class PlaywrightError(Exception):
        pass

    class PlaywrightTimeoutError(PlaywrightError):
        pass


URLS = {
    "youtube": "https://studio.youtube.com/",
    "tiktok": "https://www.tiktok.com/tiktokstudio/upload",
}
YT_CHANNEL = re.compile(r"^https://studio\.youtube\.com/channel/(UC[\w-]{22})(?:[/?#]|$)")
TT_PROFILE = re.compile(r"^/@([\w.]+)(?:/)?$", re.ASCII)
TT_HOSTS = {"www.tiktok.com", "tiktok.com", "studio.tiktok.com"}
CHALLENGE = "iframe[src*='captcha' i], #captcha-verify-container, #captcha_container, iframe[src*='challenge' i]"
VIDEO_FILE = 'input[type="file"][accept*="video" i], input[type="file"][accept*=".mp4" i], input[type="file"][accept*=".mov" i]'
FILE = 'input[type="file"]:not([accept]), input[type="file"][accept=""]'
YT_SUCCESS = re.compile(r"^(Video published|Видео опубликовано|Ролик опубликован)[.!]?$", re.I)
TT_SUCCESS = re.compile(r"^(Video published|Your video has been published|Your video has been posted|Видео опубликовано|Ваше видео опубликовано)[.!]?$", re.I)
STARTUP_PAGES = {"about:blank", "chrome://newtab/", "edge://newtab/", "opera://newtab/"}
AD_DISCLOSURES = {"youtube": ("sponsored",), "tiktok": ("own", "sponsored")}
DISCLOSURE_REQUIRED = "У ролика есть реклама. Открой готовый MP4 и опубликуй вручную с отметкой о рекламе на площадке."
DISCLOSURE_UNAVAILABLE = "Площадка не подтвердила отметку о рекламе. Проверь черновик и установи отметку вручную перед публикацией."
SHOW_MORE = r"^(Show more|Показать ещё|Показать еще|Развернуть)$"
YT_DISCLOSURE = re.compile(r"^(My video contains paid promotion(?: like a product placement, sponsorship, or endorsement)?\.?|В мо[её]м видео есть платное продвижение\.?|В видео есть платное продвижение\.?)$", re.I)
TT_DISCLOSURE = re.compile(r"^(Disclose (?:post|commercial) content|Раскрыть информацию о публикации|Раскрыть информацию о коммерческом контенте)$", re.I)
TT_DISCLOSURE_TYPES = {
    "own": re.compile(r"^(Your brand|Ваш бренд|Свой бренд)$", re.I),
    "sponsored": re.compile(r"^(Branded content|Брендированный контент)$", re.I),
}


class PostingBlocked(RuntimeError):
    pass


def validate_debug_address(value):
    try:
        url = urlsplit(value)
        if (url.scheme not in {"http", "ws"} or url.hostname not in {"127.0.0.1", "localhost", "::1"}
                or not url.port or url.username or url.password or url.query or url.fragment
                or not re.fullmatch(r"/?(?:devtools/browser/[a-f0-9-]+)?", url.path, re.I)):
            raise ValueError
    except ValueError:
        raise PostingBlocked("Укажи локальный адрес запущенного профиля Octo, например http://127.0.0.1:9222") from None
    return value


def _opera():
    roots = [Path(os.environ[name]) for name in ("LOCALAPPDATA", "ProgramFiles", "ProgramFiles(x86)") if os.environ.get(name)]
    for root in roots:
        for folder in (root / "Programs/Opera", root / "Programs/Opera GX", root / "Opera", root / "Opera GX"):
            for executable in [folder / "opera.exe", *sorted(folder.glob("*/opera.exe"), reverse=True)]:
                if executable.is_file():
                    return str(executable)
    raise PostingBlocked("Opera не найдена. Выбери Edge или запусти профиль Octo.")


async def _wait(probe, message, *, seconds=20):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        found = await probe()
        if found is not None:
            return found
        await asyncio.sleep(.3)
    raise PostingBlocked(message)


async def _one(*locators, visible=True):
    for locator in locators:
        matches = await locator.all()
        if visible:
            matches = [item for item in matches if await item.is_visible()]
        if len(matches) == 1:
            return matches[0]
    return None


async def _any_visible(locator):
    for item in await locator.all():
        if await item.is_visible():
            return True
    return False


async def _navigate(page, url):
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
    except PlaywrightError:
        raise PostingBlocked("Не удалось открыть страницу площадки. Проверь браузер и подключение, затем повтори.") from None


async def _guard(page, platform):
    url = urlsplit(page.url)
    allowed = {"studio.youtube.com"} if platform == "youtube" else TT_HOSTS
    if (url.scheme != "https" or url.hostname not in allowed
            or re.search(r"/(?:login|signin|challenge|verify)(?:[/?#]|$)", url.path, re.I)):
        raise PostingBlocked("Площадка просит войти или проверить аккаунт. Открой браузер и проверь вход.")
    for frame in page.frames:
        for element in await frame.locator(CHALLENGE).all():
            if await element.is_visible():
                raise PostingBlocked("Площадка запросила проверку. Открой браузер и пройди её вручную.")


def _button(scope, label):
    return scope.get_by_role("button", name=re.compile(label, re.I))


async def _control(page, platform, *locators, message, seconds=20, enabled=False, visible=True):
    async def find():
        await _guard(page, platform)
        target = await _one(*locators, visible=visible)
        if target is not None and enabled and not await target.is_enabled():
            return None
        return target
    return await _wait(find, message, seconds=seconds)


async def _tiktok_account(page):
    await _navigate(page, "https://www.tiktok.com/")
    links = page.locator(
        'a[data-e2e="profile-link"][href], a[data-e2e="nav-profile"][href], '
        '[data-e2e="profile-icon"] a[href], a[data-e2e="profile-icon"][href]'
    )
    menu = page.locator('header, nav, [role="menu"], [data-e2e="profile-icon"]')
    named = menu.get_by_role("link", name=re.compile(r"^(View profile|Profile|Профиль|Посмотреть профиль)$", re.I))
    avatar = page.locator('[data-e2e="profile-icon"]')
    opened = False

    async def find():
        nonlocal opened
        await _guard(page, "tiktok")
        link = await _one(links, named)
        if link is not None:
            href = await link.get_attribute("href")
            if href:
                url = urlsplit(urljoin(page.url, href))
                match = TT_PROFILE.fullmatch(url.path)
                if match and url.scheme == "https" and url.hostname in TT_HOSTS:
                    return "@" + match[1].lower()
        icon = await _one(avatar)
        if icon is not None and not opened:
            opened = True
            await icon.hover(timeout=3000)
        return None

    return await _wait(find, "Не удалось определить свой @аккаунт TikTok. Проверь вход в открытом профиле.")


async def _youtube_account(page):
    await _navigate(page, URLS["youtube"])

    async def find():
        await _guard(page, "youtube")
        match = YT_CHANNEL.match(page.url)
        return match[1] if match else None

    return await _wait(find, "Не удалось определить текущий канал в YouTube Studio.")


async def _marked(locator):
    return await locator.evaluate("""element => {
        const aria = element.getAttribute('aria-checked');
        if (aria !== null) return aria === 'true';
        if ('checked' in element) return element.checked === true;
        return element.hasAttribute('checked');
    }""")


def _requested_disclosure(platform, clip):
    disclosure = clip.get("ad_disclosure")
    if disclosure and disclosure not in AD_DISCLOSURES[platform]:
        raise PostingBlocked(DISCLOSURE_REQUIRED)
    return disclosure


def _disclosure_control(scope, label):
    return (scope.get_by_role("checkbox", name=label)
            .or_(scope.get_by_role("switch", name=label))
            .or_(scope.get_by_role("radio", name=label)))


async def _select_disclosure(page, platform, controls):
    control = await _control(page, platform, controls, message=DISCLOSURE_UNAVAILABLE, seconds=8)
    if not await _marked(control):
        if not await control.is_enabled():
            raise PostingBlocked(DISCLOSURE_UNAVAILABLE)
        await control.click(timeout=8000)

    async def selected():
        await _guard(page, platform)
        current = await _one(controls)
        return current if current is not None and await _marked(current) else None

    await _wait(selected, DISCLOSURE_UNAVAILABLE, seconds=8)


async def _show_disclosure(page, platform, scope, controls):
    if await _one(controls) is None:
        show_more = await _one(_button(scope, SHOW_MORE))
        if show_more is not None:
            await show_more.click(timeout=8000)
    await _select_disclosure(page, platform, controls)


async def _check_tiktok_disclosure(scope, disclosure):
    toggle = await _one(_disclosure_control(scope, TT_DISCLOSURE))
    choice = await _one(_disclosure_control(scope, TT_DISCLOSURE_TYPES[disclosure]))
    other_label = TT_DISCLOSURE_TYPES["sponsored" if disclosure == "own" else "own"]
    others = await _disclosure_control(scope, other_label).all()
    if (toggle is None or choice is None or not await _marked(toggle) or not await _marked(choice)
            or any([await _marked(other) for other in others])):
        raise PostingBlocked(DISCLOSURE_UNAVAILABLE)


async def _check_youtube_channel(page, account):
    await _guard(page, "youtube")
    match = YT_CHANNEL.match(page.url)
    if not match or match[1] != account:
        raise PostingBlocked("Канал в Studio изменился во время загрузки.")


async def _confirmation_url(confirmation, platform, account=None):
    scope = confirmation.locator("xpath=ancestor-or-self::*[@role='dialog' or @role='status' or self::ytcp-video-share-dialog][1]")
    links = set()
    for anchor in await scope.locator("a[href]").all():
        if not await anchor.is_visible():
            continue
        url = urlsplit(await anchor.get_attribute("href") or "")
        if url.scheme != "https":
            continue
        if platform == "youtube":
            if url.hostname == "youtu.be":
                identifier = url.path.strip("/")
            elif url.hostname in {"youtube.com", "www.youtube.com"} and url.path.startswith("/shorts/"):
                identifier = url.path.removeprefix("/shorts/").strip("/")
            elif url.hostname in {"youtube.com", "www.youtube.com"} and url.path == "/watch":
                identifier = parse_qs(url.query).get("v", [""])[0]
            else:
                continue
            if re.fullmatch(r"[A-Za-z0-9_-]{11}", identifier):
                links.add(f"https://youtu.be/{identifier}")
        elif url.hostname in TT_HOSTS:
            match = re.fullmatch(r"/@([\w.]+)/video/(\d{8,25})/?", url.path, re.ASCII)
            if match:
                if account and "@" + match[1].lower() != account.lower():
                    raise PostingBlocked("Подтверждение TikTok указывает на другой аккаунт. Проверь результат в Studio.")
                links.add(f"https://www.tiktok.com/@{match[1]}/video/{match[2]}")
    return next(iter(links)) if len(links) == 1 else ""


async def _youtube_upload(page, path, clip, account, checkpoint):
    platform = "youtube"
    disclosure = _requested_disclosure(platform, clip)
    create = await _control(page, platform, page.locator("#create-icon"),
                            _button(page, r"^(Create|Создать)$"), message="В Studio не найдена кнопка «Создать».")
    await create.click()
    upload_name = re.compile(r"^(Upload videos?|Добавить видео|Загрузить видео)$", re.I)
    upload = await _control(page, platform, page.locator("#upload-video-button"),
                            page.get_by_role("menuitem", name=upload_name),
                            page.get_by_text(upload_name), message="В Studio не найден пункт загрузки видео.")
    await upload.click()
    dialog = page.locator("ytcp-uploads-dialog")
    file_input = await _control(page, platform, dialog.locator(VIDEO_FILE), page.locator(VIDEO_FILE),
                                dialog.locator(FILE), page.locator(FILE), visible=False,
                                message="Studio не открыла однозначный выбор видео.")
    await _check_youtube_channel(page, account)
    await checkpoint("uploading")
    await file_input.set_input_files(str(path), timeout=30_000)
    title = await _control(page, platform, dialog.locator("#title-textarea #textbox"),
                           message="В Studio не открылось название ролика.", seconds=90)
    await title.fill(clip["title"][:100])
    description = await _control(page, platform, dialog.locator("#description-textarea #textbox"),
                                 message="В Studio не открылось описание ролика.")
    await description.fill(clip["caption"][:5000])
    audience = dialog.locator('tp-yt-paper-radio-button[name^="VIDEO_MADE_FOR_KIDS_"]')
    if not any([await _marked(radio) for radio in await audience.all()]):
        raise PostingBlocked("YouTube не выбрал аудиторию. Укажи аудиторию по умолчанию в настройках канала.")
    promotion = None
    if disclosure:
        promotion = _disclosure_control(dialog, YT_DISCLOSURE)
        await _show_disclosure(page, platform, dialog, promotion)

    public = dialog.locator('tp-yt-paper-radio-button[name="PUBLIC"], [role="radio"][name="PUBLIC"]')
    for _ in range(3):
        if await _one(public) is not None:
            break
        next_button = await _control(page, platform, dialog.locator("#next-button"),
                                     _button(dialog, r"^(Next|Далее)$"), enabled=True, seconds=120,
                                     message="Studio не позволяет перейти к следующему шагу.")
        await next_button.click()
    public_option = await _control(page, platform, public, message="В Studio не найден открытый доступ.")
    await public_option.click()

    async def public_selected():
        await _guard(page, platform)
        return True if await _marked(public_option) else None

    await _wait(public_selected, "Studio не подтвердила открытый доступ.", seconds=8)
    publish = await _control(page, platform, _button(dialog, r"^(Publish|Опубликовать)$"), enabled=True,
                             seconds=180, message="Studio не разрешила опубликовать ролик.")
    await _check_youtube_channel(page, account)
    if promotion is not None:
        visible_promotion = await promotion.all()
        if len(visible_promotion) > 1 or (visible_promotion and not await _marked(visible_promotion[0])):
            raise PostingBlocked(DISCLOSURE_UNAVAILABLE)
    success = page.get_by_text(YT_SUCCESS)
    if await _any_visible(success):
        raise PostingBlocked("В Studio уже открыто подтверждение публикации. Закрой его и повтори проверку.")
    await checkpoint("publishing")
    await publish.click()
    confirmation = await _control(page, platform, success, seconds=90,
                                  message="Studio не подтвердила публикацию. Проверь видео в разделе «Контент».")
    return await _confirmation_url(confirmation, platform)


def _tiktok_frames(page):
    return [frame for frame in page.frames
            if urlsplit(frame.url).scheme == "https" and urlsplit(frame.url).hostname in TT_HOSTS]


async def _selected_visibility(control):
    return await control.evaluate("""element => {
        if (element.tagName === 'SELECT') return element.selectedOptions[0]?.label?.trim() || '';
        return (element.getAttribute('aria-valuetext') ||
            element.querySelector('.tiktok-select-selection-item')?.innerText || element.innerText || '').trim();
    }""")


async def _tiktok_upload(page, path, clip, checkpoint, *, account=None):
    platform = "tiktok"
    disclosure = _requested_disclosure(platform, clip)
    await _navigate(page, URLS[platform])

    async def upload_form():
        await _guard(page, platform)
        for selector in (VIDEO_FILE, FILE):
            matches = [(frame, file_input) for frame in _tiktok_frames(page)
                       for file_input in await frame.locator(selector).all()]
            if matches:
                return matches[0] if len(matches) == 1 else None
        return None

    frame, file_input = await _wait(upload_form, "TikTok Studio не открыла выбор видео.")
    await checkpoint("uploading")
    await file_input.set_input_files(str(path), timeout=30_000)
    editor = await _control(page, platform, frame.locator('[data-e2e="caption_editor"][contenteditable="true"], [data-e2e="caption_editor"] [contenteditable="true"]'),
                            frame.locator('[contenteditable="true"][role="textbox"]'),
                            frame.locator('div[contenteditable="true"]'), seconds=120,
                            message="В TikTok Studio не появилось поле подписи.")
    await editor.fill(clip["caption"][:2200])
    public_label = re.compile(r"^(Everyone|Public|Для всех|Все|Общедоступное)$", re.I)
    visibility = await _control(page, platform, frame.locator(".tiktok-select-selector").filter(
        has_text=re.compile(r"Everyone|Public|Friends|Followers|Only you|Private|Для всех|Все|Друзья|Подписчики|Только вы|Только я", re.I)),
        frame.get_by_role("combobox").filter(
            has_text=re.compile(r"Everyone|Public|Friends|Followers|Only you|Private|Для всех|Все|Друзья|Подписчики|Только вы|Только я", re.I)),
        message="Не удалось проверить, кто увидит ролик в TikTok.")
    if not public_label.fullmatch(await _selected_visibility(visibility)):
        if await visibility.evaluate("element => element.tagName === 'SELECT'"):
            labels = [await option.evaluate("element => element.label")
                      for option in await visibility.locator("option").all() if await option.is_enabled()]
            public_labels = [label for label in labels if public_label.fullmatch(label.strip())]
            if len(public_labels) != 1:
                raise PostingBlocked("TikTok не предложил однозначный открытый доступ.")
            await visibility.select_option(label=public_labels[0])
        else:
            await visibility.click()
            public_option = await _control(page, platform, frame.get_by_role("option", name=public_label),
                                           frame.get_by_text(public_label),
                                           message="TikTok не предложил открытый доступ.")
            await public_option.click()

    async def public_selected():
        await _guard(page, platform)
        return True if public_label.fullmatch(await _selected_visibility(visibility)) else None

    await _wait(public_selected, "TikTok не подтвердил открытый доступ.", seconds=8)
    if disclosure:
        await _show_disclosure(page, platform, frame, _disclosure_control(frame, TT_DISCLOSURE))
        await _select_disclosure(page, platform, _disclosure_control(frame, TT_DISCLOSURE_TYPES[disclosure]))
    publish = await _control(page, platform, frame.locator('button[data-e2e="post_video_button"]'),
                             _button(frame, r"^(Post|Опубликовать)$"), enabled=True, seconds=180,
                             message="TikTok Studio не разрешила опубликовать ролик.")
    for root in _tiktok_frames(page):
        if await _any_visible(root.get_by_text(TT_SUCCESS)):
            raise PostingBlocked("В TikTok уже открыто подтверждение публикации. Закрой его и повтори проверку.")
    if disclosure:
        await _check_tiktok_disclosure(frame, disclosure)
    await checkpoint("publishing")
    await publish.click()

    confirmed = False
    async def result():
        nonlocal confirmed
        await _guard(page, platform)
        roots = _tiktok_frames(page)
        published = [item for root in roots for item in await root.get_by_text(TT_SUCCESS).all() if await item.is_visible()]
        if len(published) == 1:
            return await _confirmation_url(published[0], platform, account)
        if published:
            return None
        for root in roots:
            dialog = root.get_by_role("dialog")
            confirm = await _one(_button(dialog, r"^(Post now|Publish now|Опубликовать сейчас)$"))
            if confirm is not None and not confirmed:
                if disclosure:
                    await _check_tiktok_disclosure(frame, disclosure)
                confirmed = True
                await checkpoint("publishing")
                await confirm.click()
        return None

    return await _wait(result, "TikTok не показал подтверждение. Проверь публикацию в Studio.", seconds=90)


class BrowserSession:
    def __init__(self, folder):
        self.folder = Path(folder)
        self.driver = self.context = self.signature = None
        self.login_pages = {}

    async def close(self):
        if self.context is not None and self.signature[0] != "octo":
            with suppress(Exception):
                await self.context.close()
        if self.driver is not None:
            with suppress(Exception):
                await self.driver.stop()
        self.driver = self.context = self.signature = None
        self.login_pages.clear()

    async def _context(self, config):
        signature = (config["browser"], config["debug_address"])
        if self.context is not None and self.signature == signature:
            return self.context
        await self.close()
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            raise PostingBlocked("Запусти приложение через ЗАПУСК_WINDOWS.bat, чтобы установить Playwright.") from None
        try:
            self.driver = await async_playwright().start()
            browser, endpoint = signature
            if browser == "octo":
                external = await self.driver.chromium.connect_over_cdp(validate_debug_address(endpoint), timeout=10_000)
                if len(external.contexts) != 1:
                    raise PostingBlocked("Octo должен предоставить один запущенный профиль.")
                context = external.contexts[0]
            else:
                options = {"channel": "msedge"} if browser == "edge" else {"executable_path": _opera()}
                context = await self.driver.chromium.launch_persistent_context(
                    str(self.folder / browser), headless=False, timeout=45_000, **options)
        except asyncio.CancelledError:
            await self.close()
            raise
        except PostingBlocked:
            await self.close()
            raise
        except PlaywrightTimeoutError:
            await self.close()
            raise PostingBlocked("Не дождались запуска браузера. Проверь, что профиль доступен, и повтори.") from None
        except Exception:
            await self.close()
            raise PostingBlocked("Не удалось открыть браузер. Проверь, что он установлен и профиль доступен.") from None
        self.signature, self.context = signature, context
        context.on("close", lambda *_: self._closed(context))
        return context

    def _closed(self, context):
        if self.context is context:
            self.context = None
            self.login_pages.clear()

    async def open(self, platform, config):
        context = await self._context(config)
        page = self.login_pages.get(platform)
        try:
            new_login_page = page is None or page.is_closed()
            if new_login_page:
                startup = None
                if config["browser"] != "octo":
                    startup = next((tab for tab in context.pages if not tab.is_closed()
                                    and tab.url in STARTUP_PAGES and tab not in self.login_pages.values()), None)
                page = startup if startup is not None else await context.new_page()
                self.login_pages[platform] = page
            await page.bring_to_front()
            if new_login_page or page.url in STARTUP_PAGES or page.url.startswith(("chrome-error:", "edge-error:", "opera-error:")):
                # A person owns this tab. Commit is enough to display the
                # page while its sign-in redirects and resources keep loading.
                await page.goto(URLS[platform], wait_until="commit", timeout=15_000)
        except PlaywrightError:
            if page is not None and not page.is_closed():
                raise PostingBlocked("Вкладка входа осталась открыта. Проверь подключение, затем обнови её или нажми «Открыть для входа» ещё раз.") from None
            self.login_pages.pop(platform, None)
            raise PostingBlocked("Браузер не открыл вкладку входа. Проверь, что профиль запущен и доступен.") from None

    async def account(self, platform, config):
        context = await self._context(config)
        page = None
        try:
            page = await context.new_page()
            return await (_youtube_account(page) if platform == "youtube" else _tiktok_account(page))
        except PlaywrightError:
            raise PostingBlocked("Не удалось проверить аккаунт. Открой профиль и проверь, что вход выполнен.") from None
        finally:
            if page is not None:
                with suppress(Exception):
                    await page.close()

    async def publish(self, platform, config, path, clip, account, checkpoint):
        context = await self._context(config)
        page = await context.new_page()
        attached = False

        async def persist(stage):
            nonlocal attached
            if stage in {"uploading", "publishing"} and platform == "tiktok":
                if await self.account(platform, config) != account:
                    raise PostingBlocked("Текущий аккаунт TikTok изменился во время загрузки.")
                await page.bring_to_front()
                await _guard(page, platform)
            await checkpoint(stage)
            attached = True

        try:
            current = await (_youtube_account(page) if platform == "youtube" else _tiktok_account(page))
            if current != account:
                raise PostingBlocked("В браузере открыт другой аккаунт. Вернись в привязанный профиль.")
            if platform == "youtube":
                url = await _youtube_upload(page, path, clip, account, persist)
            else:
                url = await _tiktok_upload(page, path, clip, persist, account=account)
        except BaseException:
            if not attached:
                with suppress(Exception):
                    await page.close()
            raise
        with suppress(Exception):
            await page.close()
        return url
