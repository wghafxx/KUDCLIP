import { useCallback, useEffect, useState } from "react";
import { CheckCircle2, ExternalLink, Loader2, Play, Plus, RefreshCw, Trash2, XCircle } from "lucide-react";
import { toast } from "sonner";
import { API, api } from "@/lib/api";

const input = "w-full mt-1 bg-[var(--cf-bg)] border border-[var(--cf-border)] rounded-md px-3 py-2 text-sm outline-none focus:border-[var(--cf-cyan)]";
const small = "text-[11px] text-[var(--cf-text-2)]";
const err = (e, fallback) => (typeof e?.response?.data?.detail === "string" ? e.response.data.detail : fallback);
const PLATFORMS = { tiktok: "TikTok", youtube: "YouTube Shorts" };
const POST_STATUS = { published: "Опубликовано", blocked: "Не опубликовано", needs_review: "Проверь вручную", preparing: "Готовится", uploading: "Загружается", publishing: "Публикуется" };
const DAY_STATUS = { planning: "Ищу исходники", processing: "Монтаж", publishing: "Публикация", ready: "Клипы в запасе", nothing: "Сегодня без роликов" };

function Schedule({ onRun }) {
  const [cfg, setCfg] = useState(null);
  useEffect(() => { api.get("/authors/schedule").then((r) => setCfg(r.data)).catch(() => toast.error("Не загрузилось расписание")); }, []);
  const save = async (patch) => {
    try { setCfg((await api.put("/authors/schedule", patch)).data); toast.success("Расписание сохранено"); }
    catch (e) { toast.error(err(e, "Не сохранилось")); }
  };
  if (!cfg) return <Loader2 className="w-4 h-4 animate-spin" />;
  const time = `${String(cfg.hour).padStart(2, "0")}:${String(cfg.minute).padStart(2, "0")}`;
  return <section className="cf-card p-5 space-y-4" data-testid="author-schedule">
    <div className="flex flex-wrap items-center justify-between gap-3">
      <div>
        <h2 className="font-display font-semibold">Ежедневный запуск</h2>
        <p className={small}>Каждый день в {time} по времени этого ПК программа по очереди проверяет каждого автора.</p>
      </div>
      <div className="flex flex-wrap gap-2">
        <button data-testid="authors-run-reserve-btn" className="ghost-btn rounded-md px-4 py-2 text-xs" onClick={() => onRun(true)}>Подготовить в запас</button>
        <button data-testid="authors-run-now-btn" className="neon-btn rounded-md px-4 py-2 text-xs flex items-center gap-2" onClick={() => onRun(false)}><Play className="w-3.5 h-3.5" />Запустить сейчас</button>
      </div>
    </div>
    <div className="grid grid-cols-2 md:grid-cols-4 gap-3 text-xs">
      <label className="flex items-center gap-2 col-span-2 md:col-span-1"><input data-testid="authors-enabled" type="checkbox" checked={cfg.enabled} onChange={(e) => save({ enabled: e.target.checked })} />Включено</label>
      <label>Время<input data-testid="authors-time" type="time" className={input} defaultValue={time} onBlur={(e) => { const [h, m] = e.target.value.split(":").map(Number); if (!Number.isNaN(h)) save({ hour: h, minute: m }); }} /></label>
      <label>Свежесть, ч<input data-testid="authors-fresh" type="number" min="6" max="96" className={input} defaultValue={cfg.fresh_hours} onBlur={(e) => save({ fresh_hours: Number(e.target.value) })} /></label>
      <label>В автопост с исходника
        <select data-testid="authors-per-source" className={input} value={cfg.clips_per_source} onChange={(e) => save({ clips_per_source: Number(e.target.value) })}><option value={1}>1</option><option value={2}>до 2</option></select>
      </label>
      <label>Клипов с YouTube (до 3 ч)
        <select data-testid="authors-youtube-clips" className={input} value={cfg.youtube_clips ?? 6} onChange={(e) => save({ youtube_clips: Number(e.target.value) })}>{[4, 5, 6, 7, 8].map((n) => <option key={n} value={n}>до {n}</option>)}</select>
      </label>
      <label>Клипов с Twitch (до 7 ч)
        <select data-testid="authors-twitch-clips" className={input} value={cfg.twitch_clips ?? 8} onChange={(e) => save({ twitch_clips: Number(e.target.value) })}>{[7, 8, 9, 10].map((n) => <option key={n} value={n}>до {n}</option>)}</select>
      </label>
      <label>Пауза между постами, мин<input data-testid="authors-gap" type="number" min="20" max="720" className={input} defaultValue={cfg.post_interval_minutes} onBlur={(e) => save({ post_interval_minutes: Number(e.target.value) })} /></label>
    </div>
    <p className={small}>Готовим клипы из последних завершённых исходников. Лучшие попадают в очередь автопоста, остальные доступны для просмотра и скачивания в запасе.</p>
  </section>;
}

function Account({ profileId, platform, state, reload }) {
  const [busy, setBusy] = useState("");
  const account = state?.accounts?.[platform];
  const enabled = Boolean(state?.enabled_since?.[platform]);
  const act = async (name, call) => {
    setBusy(name);
    try { await call(); await reload(); }
    catch (e) { toast.error(err(e, "Не получилось")); }
    finally { setBusy(""); }
  };
  const base = `/authors/${profileId}/posting/${platform}`;
  return <div className="border border-[var(--cf-border)] rounded-md p-3 space-y-2" data-testid={`account-${profileId}-${platform}`}>
    <div className="flex items-center justify-between text-xs"><b>{PLATFORMS[platform]}</b>
      <span className={account ? "text-emerald-400" : "text-amber-400"}>{account ? String(account) : "не привязан"}</span></div>
    <div className="flex flex-wrap gap-2 text-[11px]">
      <button data-testid={`open-${profileId}-${platform}`} className="ghost-btn rounded px-2 py-1" disabled={!!busy} onClick={() => act("open", () => api.post(`${base}/open`))}>{busy === "open" ? "Открываю…" : "1. Открыть окно и войти"}</button>
      <button data-testid={`bind-${profileId}-${platform}`} className="ghost-btn rounded px-2 py-1" disabled={!!busy} onClick={() => act("bind", () => api.post(`${base}/bind`))}>{busy === "bind" ? "Проверяю…" : "2. Проверить вход"}</button>
      <label className="flex items-center gap-1"><input data-testid={`autopost-${profileId}-${platform}`} type="checkbox" disabled={!account || !!busy} checked={enabled} onChange={(e) => act("toggle", () => api.put(base, { enabled: e.target.checked }))} />3. Автопост</label>
    </div>
  </div>;
}

function AuthorCard({ profile, onChanged, onDelete }) {
  const s = profile.settings || {};
  const [posting, setPosting] = useState(null);
  const reload = useCallback(async () => { try { setPosting((await api.get(`/authors/${profile.id}/posting`)).data); } catch { setPosting({}); } }, [profile.id]);
  useEffect(() => { reload(); }, [reload]);
  const patch = async (body) => {
    try { onChanged((await api.patch(`/profiles/${profile.id}`, body)).data); }
    catch (e) { toast.error(err(e, "Не сохранилось")); }
  };
  const setting = (key) => (value) => patch({ settings: { [key]: value } });
  const proxy = async (value) => {
    try { setPosting((await api.put(`/authors/${profile.id}/proxy`, { proxy: value })).data); toast.success("Прокси сохранён"); }
    catch (e) { toast.error(err(e, "Прокси в формате IP:порт")); }
  };
  return <article className="cf-card p-5 space-y-4" data-testid={`author-card-${profile.id}`}>
    <div className="flex items-center gap-3">
      <input data-testid={`author-name-${profile.id}`} className={`${input} font-semibold`} defaultValue={profile.name} onBlur={(e) => e.target.value.trim() && e.target.value !== profile.name && patch({ name: e.target.value.trim() })} />
      <label className="flex items-center gap-1 text-xs shrink-0"><input type="checkbox" checked={s.daily_enabled !== false} onChange={(e) => setting("daily_enabled")(e.target.checked)} />В работе</label>
      <button data-testid={`author-delete-${profile.id}`} className="text-red-400 shrink-0" title="Удалить автора" onClick={() => window.confirm(`Удалить автора «${profile.name}»?`) && onDelete(profile.id)}><Trash2 className="w-4 h-4" /></button>
    </div>
    <div className="grid md:grid-cols-2 gap-3 text-xs">
      <label>Twitch автора<input data-testid={`author-twitch-${profile.id}`} className={input} placeholder="https://www.twitch.tv/…" defaultValue={(profile.twitch_channels || [])[0] || ""}
        onBlur={(e) => patch({ twitch_channels: e.target.value.trim() ? [e.target.value.trim()] : [], settings: { twitch_trending: Boolean(e.target.value.trim()) } })} /></label>
      <label>YouTube автора<input data-testid={`author-youtube-${profile.id}`} className={input} placeholder="https://www.youtube.com/@…" defaultValue={(profile.channels || [])[0] || ""}
        onBlur={(e) => patch({ channels: e.target.value.trim() ? [e.target.value.trim()] : [] })} /></label>
    </div>
    <div className="flex flex-wrap items-end gap-4 text-xs">
      <label className="flex items-center gap-2"><input type="checkbox" checked={s.title_banner !== false} onChange={(e) => setting("title_banner")(e.target.checked)} />Баннер с заголовком</label>
      <label>Подпись на баннере<input className={input} placeholder={profile.name} defaultValue={s.brand_label || ""} onBlur={(e) => setting("brand_label")(e.target.value.trim())} /></label>
      {[["brand_color", "Акцент", "#22DDEB"], ["brand_highlight", "Последняя строка", "#BFF84F"], ["brand_panel", "Фон", "#09101D"]].map(([key, label, def]) =>
        <label key={key}>{label}<input type="color" className="block w-10 h-8 mt-1 rounded" defaultValue={s[key] || def} onBlur={(e) => setting(key)(e.target.value.toUpperCase())} /></label>)}
      <div className="rounded-lg px-3 py-2 font-black text-[11px] leading-tight" style={{ background: s.brand_panel || "#09101D", borderLeft: `4px solid ${s.brand_color || "#22DDEB"}` }} data-testid={`brand-preview-${profile.id}`}>
        <span style={{ color: s.brand_color || "#22DDEB" }}>{(s.brand_label || profile.name).toUpperCase()}</span><br />ОН РЕАЛЬНО ЭТО<br /><span style={{ color: s.brand_highlight || "#BFF84F" }}>СДЕЛАЛ?!</span>
      </div>
    </div>
    <div className="grid md:grid-cols-2 gap-3">{Object.keys(PLATFORMS).map((p) => <Account key={p} profileId={profile.id} platform={p} state={posting} reload={reload} />)}</div>
    <details className="text-xs"><summary className="cursor-pointer text-[var(--cf-text-2)]">Защита аккаунтов</summary>
      <label className="block mt-2">Прокси этого автора (необязательно, IP:порт)<input data-testid={`author-proxy-${profile.id}`} className={input} placeholder="socks5://1.2.3.4:1080" defaultValue={posting?.proxy || ""} onBlur={(e) => e.target.value !== (posting?.proxy || "") && proxy(e.target.value.trim())} /></label>
      <p className={`${small} mt-1`}>У автора своё окно браузера со своими куками. Свой прокси — чтобы аккаунты разных авторов не выглядели как один человек.</p>
    </details>
  </article>;
}

const PostBadge = ({ post }) => <a href={post.url || undefined} target="_blank" rel="noreferrer"
  className={`inline-flex items-center gap-1 rounded px-2 py-0.5 ${post.status === "published" ? "bg-emerald-500/15 text-emerald-300" : post.status === "blocked" || post.status === "needs_review" ? "bg-red-500/15 text-red-300" : "bg-white/5"}`}
  title={post.message}>{post.status === "published" ? <CheckCircle2 className="w-3 h-3" /> : post.status === "blocked" ? <XCircle className="w-3 h-3" /> : null}
  {PLATFORMS[post.platform]}: {POST_STATUS[post.status] || post.status}{post.message && post.status !== "published" ? ` — ${post.message}` : ""}</a>;

const ClipLink = ({ clipId }) => <a className="underline text-[var(--cf-cyan)]" href={`${API}/clips/${clipId}/file`} target="_blank" rel="noreferrer">Открыть MP4</a>;

function Reports({ refreshKey }) {
  const [days, setDays] = useState(null);
  const load = useCallback(async () => { try { setDays((await api.get("/authors/reports")).data.days); } catch { setDays([]); } }, []);
  useEffect(() => { load(); const t = setInterval(load, 20000); return () => clearInterval(t); }, [load, refreshKey]);
  if (!days) return <Loader2 className="w-4 h-4 animate-spin" />;
  return <section className="cf-card p-5 space-y-4" data-testid="author-reports">
    <div className="flex items-center justify-between"><h2 className="font-display font-semibold">Отчёты</h2><button className="ghost-btn rounded p-1.5" onClick={load} title="Обновить"><RefreshCw className="w-3.5 h-3.5" /></button></div>
    {!days.length && <p className={small}>Отчёты появятся после первого запуска.</p>}
    {days.map((row) => <div key={row.id} className="border border-[var(--cf-border)] rounded-lg p-4 space-y-3 text-xs" data-testid={`report-${row.id}`}>
      <div className="flex justify-between"><b>{row.author} · {row.day}</b><span className="text-[var(--cf-cyan)]">{DAY_STATUS[row.status] || row.status}</span></div>
      {row.sources.map((src) => <div key={src.platform} className="space-y-1">
        <p><b>{src.platform === "twitch" ? "Twitch" : "YouTube"}:</b> {src.url ? <a className="underline inline-flex items-center gap-1" href={src.url} target="_blank" rel="noreferrer">{src.title}<ExternalLink className="w-3 h-3" /></a> : "—"}</p>
        {src.reason && <p className={small}>{src.reason}</p>}
        {src.error && <p className="text-red-400">Ошибка: {src.error}</p>}
        {src.requested_clips && src.status === "done" ? <p className={small}>Готово клипов: {src.prepared_clips ?? 0} из {src.requested_clips}</p> : null}
        {!!src.timecodes?.length && <p className="flex flex-wrap gap-2">{src.timecodes.map((t) => <a key={t.start} href={t.link} target="_blank" rel="noreferrer" className="underline text-[var(--cf-cyan)]">{new Date(t.start * 1000).toISOString().substring(11, 19)}</a>)}</p>}
      </div>)}
      {!!row.releases?.length && <div className="space-y-2 border-t border-[var(--cf-border)] pt-2">{row.releases.map((rel) => <div key={rel.clip_id}>
        <p>«{rel.title}» <span className={small}>({rel.platform}, оценка {Math.round(rel.quality || 0)})</span> <ClipLink clipId={rel.clip_id} /></p>
        <div className="flex flex-wrap gap-2 mt-1">{rel.posts.length ? rel.posts.map((p) => <PostBadge key={p.platform} post={p} />) : <span className={small}>ждёт очереди на публикацию</span>}</div>
      </div>)}</div>}
      {!!row.reserve?.length && <details className="border-t border-[var(--cf-border)] pt-2">
        <summary className="cursor-pointer">Запас готовых клипов ({row.reserve.length})</summary>
        <div className="mt-2 space-y-1">{row.reserve.map((clip) => <p key={clip.clip_id}>«{clip.title}» <span className={small}>({clip.platform})</span> <ClipLink clipId={clip.clip_id} /></p>)}</div>
      </details>}
      {row.skipped?.map((s) => <p key={s.clip_id} className={small}>Пропущен «{s.title}»: {s.reason}</p>)}
    </div>)}
  </section>;
}

export default function AuthorsPage({ profiles, setProfiles }) {
  const [refresh, setRefresh] = useState(0);
  const add = async () => {
    try { const { data } = await api.post("/profiles", { name: `Автор ${profiles.length + 1}` }); setProfiles((prev) => [...prev, data]); }
    catch (e) { toast.error(err(e, "Не удалось добавить автора")); }
  };
  const remove = async (id) => {
    try { await api.delete(`/profiles/${id}`); setProfiles((prev) => prev.filter((p) => p.id !== id)); }
    catch (e) { toast.error(err(e, "Не удалось удалить")); }
  };
  const run = async (reserveOnly) => {
    try { await api.post("/authors/run", { reserve_only: reserveOnly }); toast.success(reserveOnly ? "Запущена подготовка в запас" : "Запущена проверка авторов"); setTimeout(() => setRefresh((v) => v + 1), 30000); }
    catch (e) { toast.error(err(e, "Не удалось запустить")); }
  };
  const changed = (data) => setProfiles((prev) => prev.map((p) => (p.id === data.id ? data : p)));
  return <div className="space-y-6" data-testid="authors-page">
    <Schedule onRun={run} />
    <div className="flex items-center justify-between"><h2 className="font-display font-semibold">Авторы ({profiles.length}/10)</h2>
      <button data-testid="add-author-btn" className="ghost-btn rounded-md px-3 py-1.5 text-xs flex items-center gap-1" onClick={add}><Plus className="w-3.5 h-3.5" />Добавить автора</button></div>
    {profiles.map((p) => <AuthorCard key={p.id} profile={p} onChanged={changed} onDelete={remove} />)}
    <Reports refreshKey={refresh} />
  </div>;
}
