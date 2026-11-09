import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";

const ROOT = "/browser-posting";
const platforms = { youtube: "YouTube Shorts", tiktok: "TikTok" };
const statuses = {
  preparing: "Готовится к публикации",
  uploading: "Загружается",
  publishing: "Публикуется",
  published: "Опубликовано",
  blocked: "Остановлено до загрузки",
  needs_review: "Проверьте на площадке",
};
const buttonClass = "ghost-btn rounded-md px-3 py-2 text-xs disabled:opacity-50";
const fieldClass = "bg-[var(--cf-bg)] border border-[var(--cf-border)] rounded-md px-3 py-2 text-xs disabled:opacity-50";

function PostRecord({ post }) {
  return <li className="space-y-1 rounded-md bg-[var(--cf-bg)] px-3 py-2 text-xs text-[var(--cf-text-2)]">
    <p className="font-semibold text-[var(--cf-text)] break-words">{post.title || "Ролик"}</p>
    <p>{statuses[post.status] || post.status}{post.day && <> · <time dateTime={post.day}>{post.day}</time></>}</p>
    {post.message && <p className="text-amber-400">{post.message}</p>}
    {post.url && <a className="underline" href={post.url} target="_blank" rel="noreferrer">Открыть ролик</a>}
    {["24h", "48h"].map((period) => {
      const views = post.view_samples?.[period]?.views;
      return Number.isInteger(views) && views >= 0
        ? <p key={period}>Просмотры через {period.slice(0, 2)} ч · {views.toLocaleString("ru-RU")}</p>
        : null;
    })}
  </li>;
}

export default function BrowserPostingPanel() {
  const [state, setState] = useState(null);
  const [draft, setDraft] = useState(null);
  const [busy, setBusy] = useState(false);
  const [loadError, setLoadError] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const lifecycle = useRef({ mounted: false, running: false, request: null });

  const refresh = useCallback(async () => {
    const session = lifecycle.current;
    if (!session.mounted || session.running) return;
    session.request?.abort();
    const request = new AbortController();
    session.request = request;
    try {
      const { data } = await api.get(ROOT, { signal: request.signal });
      if (session.mounted && session.request === request) {
        setState(data);
        setLoadError("");
      }
    } catch (err) {
      if (session.mounted && session.request === request) {
        const detail = err?.response?.data?.detail;
        setLoadError(typeof detail === "string" ? detail : "Не удалось обновить состояние автопоста.");
      }
    } finally {
      if (session.request === request) session.request = null;
    }
  }, []);

  useEffect(() => {
    const session = lifecycle.current;
    session.mounted = true;
    let stopped = false;
    let timer;
    async function poll() {
      await refresh();
      if (!stopped) timer = setTimeout(poll, 5000);
    }
    poll();
    return () => {
      stopped = true;
      session.mounted = false;
      clearTimeout(timer);
      session.request?.abort();
      session.request = null;
    };
  }, [refresh]);

  async function action(method, path, body, success = "") {
    const session = lifecycle.current;
    if (session.running) return;
    session.running = true;
    session.request?.abort();
    session.request = null;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const { data } = await api[method](ROOT + path, body);
      if (!session.mounted) return;
      if (data.accounts) {
        setState(data);
        setLoadError("");
      }
      if (method === "put" && path === "") setDraft(null);
      setNotice(success);
    } catch (err) {
      const detail = err?.response?.data?.detail;
      if (session.mounted) {
        setError(typeof detail === "string" ? detail : "Не удалось завершить действие.");
      }
    } finally {
      session.running = false;
      if (session.mounted) setBusy(false);
    }
  }

  const browser = draft?.browser ?? state?.browser ?? "edge";
  const address = draft?.address ?? state?.debug_address ?? "";
  const debugAddress = browser === "octo" ? address.trim() : "";
  const unsaved = Boolean(state && (browser !== state.browser || debugAddress !== state.debug_address));
  const configured = state && !busy && !unsaved;
  const hasBindings = Object.values(state?.accounts ?? {}).some(Boolean);

  return <section className="cf-card p-5 space-y-4" aria-label="Автопост через браузер">
    <div>
      <h2 className="font-display text-lg font-semibold">Автопост через браузер</h2>
      <p className="text-xs text-[var(--cf-text-2)] mt-2">
        Войдите на площадке, привяжите один аккаунт и включите автопост. В открытый доступ отправляются ежедневные выпуски, подготовленные после включения.
      </p>
    </div>
    <form className="flex flex-wrap gap-2 items-end" onSubmit={(event) => {
      event.preventDefault();
      if (unsaved) action("put", "", { browser, debug_address: debugAddress }, "Браузер сохранён.");
    }}>
      <label className="flex flex-col gap-1 text-xs">Браузер
        <select className={fieldClass} value={browser} disabled={busy || !state}
          onChange={(event) => setDraft({ browser: event.target.value, address })}>
          <option value="edge">Microsoft Edge</option>
          <option value="opera">Opera (если установлена)</option>
          <option value="octo">Открытый профиль Octo</option>
        </select>
      </label>
      {browser === "octo" && <label className="flex flex-col gap-1 text-xs">Локальный адрес профиля
        <input className={fieldClass} value={address} disabled={busy || !state}
          onChange={(event) => setDraft({ browser, address: event.target.value })}
          placeholder="http://127.0.0.1:9222" maxLength={200} required />
      </label>}
      <button type="submit" className={buttonClass} disabled={busy || !unsaved}>Сохранить браузер</button>
      {unsaved && <button type="button" className={buttonClass} disabled={busy} onClick={() => setDraft(null)}>Отменить изменения</button>}
    </form>
    {unsaved && hasBindings && <p className="text-xs text-amber-400">
      Сохранение нового браузера или адреса Octo отключит автопост и сбросит привязки. В новом профиле привяжите аккаунты снова.
    </p>}
    <div className="grid gap-3 sm:grid-cols-2">
      {Object.entries(platforms).map(([platform, label]) => {
        const account = state?.accounts?.[platform];
        const enabledSince = state?.enabled_since?.[platform];
        const enabled = Boolean(enabledSince);
        const posts = (state?.posts ?? []).filter((post) => account && post.platform === platform && post.account === account);
        const today = posts.filter((post) => post.day === state?.day);
        const canRetry = enabled && today.some((post) => post.status === "blocked" && post.retry_allowed !== false
          && (post.ready_at == null || post.ready_at > enabledSince));
        const recent = [...posts]
          .sort((a, b) => (b.day ?? "").localeCompare(a.day ?? "") || (b.updated_at ?? 0) - (a.updated_at ?? 0))
          .slice(0, 3);
        const path = `/${platform}`;
        return <div key={platform} className="p-3 rounded-lg border border-[var(--cf-border)] space-y-2">
          <h3 className="text-sm font-semibold">{label}</h3>
          <p className="text-xs text-[var(--cf-text-2)] break-all">
            {state ? account || "Аккаунт ещё не привязан" : "Состояние пока недоступно"}
          </p>
          <div className="flex flex-wrap gap-2">
            <button type="button" className={buttonClass} disabled={!configured} onClick={() =>
              action("post", path + "/open", {}, "Войдите в открывшемся окне, затем нажмите «Привязать аккаунт».")}>Открыть для входа</button>
            <button type="button" className={buttonClass} disabled={!configured} onClick={() =>
              action("post", path + "/bind", {}, "Аккаунт привязан.")}>Привязать аккаунт</button>
          </div>
          <label className="flex items-center gap-2 text-xs">
            <input type="checkbox" disabled={!configured || !account} checked={enabled} onChange={(event) => {
              const nextEnabled = event.target.checked;
              const message = nextEnabled ? "Автопост включён для следующих выпусков." : "Автопост отключён.";
              action("put", path, { enabled: nextEnabled }, message);
            }} />
            Автопост в открытый доступ
          </label>
          {platform === "youtube" && <p className="text-xs text-[var(--cf-text-2)]">
            Укажите аудиторию по умолчанию в настройках канала YouTube Studio.
          </p>}
          {recent.length > 0 && <ul className="space-y-2" aria-label={`Последние выпуски ${label}`}>
            {recent.map((post) => <PostRecord key={post.id || `${post.day}:${post.clip_id || post.title}`} post={post} />)}
          </ul>}
          {canRetry && <button type="button" className={buttonClass} disabled={!configured} onClick={() =>
            action("post", path + "/retry", {}, "Повтор поставлен в очередь.")}>Повторить отправку</button>}
          {enabled && today.length === 0 && <p className="text-xs text-[var(--cf-text-2)]">Ждёт нового ежедневного выпуска.</p>}
        </div>;
      })}
    </div>
    {!state && !loadError && <p role="status" className="text-xs text-[var(--cf-text-2)]">Загружаю состояние автопоста…</p>}
    {loadError && <div role="alert" className="text-xs text-red-400 flex flex-wrap items-center gap-2">
      {loadError}<button type="button" className={buttonClass} onClick={refresh} disabled={busy}>Обновить</button>
    </div>}
    {error && <p role="alert" className="text-xs text-red-400">{error}</p>}
    {(busy || notice) && <p role="status" className="text-xs text-[var(--cf-text-2)]">{busy ? "Выполняю действие…" : notice}</p>}
  </section>;
}
