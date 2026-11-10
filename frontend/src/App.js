import { useCallback, useEffect, useRef, useState } from "react";
import "@/App.css";
import { Toaster, toast } from "sonner";
import {
  Plus, Trash2, Settings2, Clock, Send, Copy,
  Download, Loader2, RefreshCw, CheckCircle2, XCircle, Play, Archive, X, KeyRound, Rocket,
} from "lucide-react";
import { api, API, FONT_OPTIONS, fontFamily } from "@/lib/api";
import BrowserPostingPanel from "@/BrowserPostingPanel";
import ProfileMediaPanel from "@/ProfileMediaPanel";
import ProfileSources from "@/ProfileSources";
import StyleSettings from "@/StyleSettings";
import AiSettingsPanel from "@/AiSettingsPanel";
import AuthorsPage from "@/AuthorsPage";

const errText = (e, fallback) => {
  const d = e?.response?.data?.detail;
  if (typeof d === "string" && d.trim() && !d.startsWith("<")) return d;
  if (Array.isArray(d)) return d.map((x) => x?.msg || String(x)).join("; ");
  return fallback;
};
const mergePatch = (base, patch) => ({
  ...base, ...patch,
  ...((base?.settings || patch?.settings) ? { settings: { ...base?.settings, ...patch?.settings } } : {}),
});
const TERMINAL_JOBS = new Set(["done", "error", "cancelled", "review"]);
const JOB_LABELS = {
  queued: "В очереди", waiting_quota: "Ожидание бесплатной квоты", analyzing: "Отбор моментов",
  transcribing: "Распознавание речи", rendering: "Сборка клипов", downloading: "Скачивание",
  sending: "Отправка", packaging: "Подготовка комплекта", done: "Готово", error: "Ошибка", cancelled: "Отменено", review: "Готово",
};
const inputClass = "w-full mt-1 bg-[var(--cf-bg)] border border-[var(--cf-border)] rounded-md px-3 py-2 text-sm outline-none focus:border-[var(--cf-cyan)]";

/* ---------------- Advanced profile setup ---------------- */
const ProfileManager = ({ profiles, activeId, onSelect, onCreate, onDelete }) => {
  const [name, setName] = useState("");
  return <div className="cf-card p-5">
    <label className="text-xs text-[var(--cf-text-2)]" htmlFor="active-profile">Источники и оформление</label>
    <div className="flex gap-2 mt-2 flex-wrap">
      {profiles.map((p) => <button key={p.id} data-testid={`profile-item-${p.id}`} onClick={() => onSelect(p.id)} className={`rounded-md px-3 py-2 text-xs border ${activeId === p.id ? "border-[var(--cf-cyan)] text-[var(--cf-cyan)]" : "ghost-btn"}`}>{p.name}</button>)}
    </div>
    <details className="mt-3 text-xs text-[var(--cf-text-2)]"><summary className="cursor-pointer">Управление профилями</summary><form className="flex gap-2 mt-3" onSubmit={(e) => { e.preventDefault(); if (name.trim()) { onCreate(name.trim()); setName(""); } }}><input data-testid="new-profile-name-input" value={name} maxLength={100} onChange={(e) => setName(e.target.value)} placeholder="Название профиля" className={inputClass} /><button data-testid="create-profile-submit" className="ghost-btn rounded px-3 text-xs" type="submit">Добавить</button></form>{activeId && <button onClick={() => onDelete(activeId)} className="text-red-400 underline mt-3">Удалить выбранный профиль</button>}</details>
  </div>;
};

const formatTimestamp = (value, timeZone) => {
  if (!value) return "—";
  const candidate = typeof value === "object" ? value.finished_at || value.started_at || value.at || value.date : value;
  if (!candidate) return "—";
  const date = new Date(typeof candidate === "number" ? (candidate > 1e12 ? candidate : candidate * 1000) : candidate);
  if (Number.isNaN(date.getTime())) return "—";
  try { return date.toLocaleString("ru-RU", { timeZone: timeZone || "Asia/Qyzylorda", day: "numeric", month: "long", hour: "2-digit", minute: "2-digit" }); }
  catch { return date.toLocaleString("ru-RU"); }
};

const sourceProviders = { youtube: "YouTube", twitch: "Twitch" };
const publicSource = (value) => {
  try {
    const url = new URL(value);
    if (url.protocol === "https:" && !url.username && !url.password && !url.port) {
      if (["www.youtube.com", "youtube.com", "m.youtube.com", "youtu.be"].includes(url.hostname)) {
        return { provider: "YouTube", href: url.href };
      } else if (["www.twitch.tv", "twitch.tv", "clips.twitch.tv"].includes(url.hostname)) {
        return { provider: "Twitch", href: url.href };
      }
    }
  } catch { /* Older entries may have no original URL. */ }
  return {};
};

const hourlyViews = (value) => value != null && Number.isFinite(value) && value >= 0
  ? value.toLocaleString("ru-RU", { maximumFractionDigits: 1 })
  : "";

const SourceDetails = ({ item, timeZone }) => {
  const source = publicSource(item.source_url || item.video_url);
  const provider = sourceProviders[item.source_provider] || source.provider;
  const trend = typeof item.source_trend === "string" ? item.source_trend.trim() : "";
  const views = hourlyViews(item.source_views_per_hour);
  const published = formatTimestamp(item.source_published_at, timeZone);
  if (!provider && !trend && !views && published === "—") return null;

  return <div className="space-y-1 text-xs text-[var(--cf-text-2)]" data-testid={`source-details-${item.id}`}>
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
      {provider && <span>{provider}</span>}
      {published !== "—" && <span>Исходник опубликован {published}</span>}
      {views && <span>Просмотры в час · {views}</span>}
      {source.href && <a className="text-[var(--cf-cyan)] underline" href={source.href} target="_blank" rel="noreferrer">Оригинал</a>}
    </div>
    {trend && <p>{/^\d+\/100$/.test(trend) ? `Оценка тренда ${trend}` : trend}</p>}
  </div>;
};

const SourceSuggestions = ({ candidates }) => {
  const suggestions = new Map();
  for (const candidate of candidates || []) {
    const video = candidate?.video;
    if (video?.discovery_only !== true) continue;
    const { provider, href } = publicSource(video.url);
    if (provider === "YouTube" && !suggestions.has(href)) suggestions.set(href, video);
    if (suggestions.size === 5) break;
  }
  if (!suggestions.size) return null;
  return <details className="mt-4 text-xs text-[var(--cf-text-2)]" data-testid="source-suggestions">
    <summary className="cursor-pointer">Предложения из YouTube</summary>
    <p className="mb-2">Автопилот анализирует видео только из каналов, которые вы добавили в источники.</p>
    <ul className="space-y-2">
      {Array.from(suggestions, ([href, video]) => {
        const channel = video.channel_title || video.channel || video.channel_name || video.uploader || video.channel_url;
        const views = hourlyViews(video.views_per_hour);
        return <li key={href}>
          <a className="text-[var(--cf-cyan)] underline" href={href} target="_blank" rel="noreferrer">{video.title || "Открыть видео"}</a>
          <p>{channel}{channel && views && " · "}{views && `Просмотры в час · ${views}`}</p>
        </li>;
      })}
    </ul>
  </details>;
};

const AutopilotPanel = ({ config, onRun, busy, activeJobs, profiles }) => {
  const schedule = `${String(config?.hour ?? 10).padStart(2, "0")}:${String(config?.minute ?? 0).padStart(2, "0")}`;
  const preparing = busy || activeJobs.length > 0 || ["running", "selecting", "enqueuing", "searching", "preparing"].includes(config?.status);
  const currentJob = activeJobs[0];
  const dailyTarget = config?.daily_target ?? 3;
  const counters = config?.counters;
  const waitingTrend = config?.status === "waiting_trend";
  const nextScan = config?.next_scan_at ?? config?.next_scan ?? config?.last_run?.next_scan_at ?? config?.last_run?.retry_at;
  const dailyProfiles = profiles.filter((item) => item.settings?.daily_enabled !== false);
  const providers = [];
  if (dailyProfiles.some((item) => item.channels?.length)) providers.push("YouTube за последние 48 часов");
  if (dailyProfiles.some((item) => item.settings?.twitch_trending && item.twitch_channels?.length)) {
    providers.push("Хайповые записи стримов и клипы Twitch");
  }
  return <section className="cf-card autopilot-panel p-6 md:p-8" data-testid="autopilot-panel">
    <div className="flex justify-between items-start gap-5 flex-wrap">
      <div className="space-y-3">
        <div className="flex items-center gap-2">
          <Rocket className="w-4 h-4 text-[var(--cf-cyan)]" />
          <h2 className="font-display font-semibold text-lg">Автопилот</h2>
          <span className={`text-[10px] border rounded-full px-2 py-0.5 ${config?.enabled ? "border-emerald-400/40 text-emerald-400" : "border-[var(--cf-border)] text-[var(--cf-text-2)]"}`}>
            {config ? config.enabled ? "Включён" : "На паузе" : "Подключение…"}
          </span>
        </div>
        <p className="text-2xl md:text-3xl font-semibold">До {dailyTarget} {dailyTarget === 1 ? "ролика" : "роликов"} в день</p>
        <p className="text-sm text-[var(--cf-text-2)]">Ролик, обложка и подпись в одном комплекте.</p>
        {providers.length > 0 && <p className="text-xs text-[var(--cf-text-2)]" data-testid="autopilot-sources">
          {providers.join(" · ")}
        </p>}
      </div>
      <button data-testid="run-autopilot" onClick={onRun} disabled={preparing || !config} className="neon-btn rounded-lg px-5 py-3 text-sm flex items-center gap-2 disabled:opacity-50">{preparing ? <Loader2 className="w-4 h-4 animate-spin" /> : <Play className="w-4 h-4" />}{preparing ? "Готовится ролик…" : "Подготовить ролик сейчас"}</button>
    </div>
    <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mt-7 pt-5 border-t border-[var(--cf-border)] text-xs">
      <div><p className="text-[var(--cf-text-2)]">Расписание</p><p className="mt-1 font-semibold">{config?.continuous ? "Круглосуточно" : `Каждый день в ${schedule}`}</p></div>
      <div><p className="text-[var(--cf-text-2)]">{waitingTrend ? "Следующая проверка" : "Следующий запуск"}</p><p className="mt-1 font-semibold">
        {config?.enabled ? formatTimestamp(waitingTrend ? nextScan : config.next_run_at, config.time_zone) : "Автопилот на паузе"}
      </p></div>
      <div><p className="text-[var(--cf-text-2)]">Готово сегодня</p><p className="mt-1 font-semibold" data-testid="daily-output-count">
        {counters?.outputs ?? "—"} из {dailyTarget}
      </p></div>
      <div><p className="text-[var(--cf-text-2)]">Начато обработок</p><p className="mt-1 font-semibold" data-testid="daily-source-count">
        {counters?.sources_started ?? "—"}{config?.max_source_videos ? ` из ${config.max_source_videos}` : ""}
      </p></div>
    </div>
    {currentJob && <div className="mt-5 space-y-2" role="status">
      <p className="text-xs text-[var(--cf-text-2)]">{currentJob.status_text || JOB_LABELS[currentJob.status] || "Готовится выпуск"}</p>
      <SourceDetails item={currentJob} timeZone={config?.time_zone} />
      <div className="h-1.5 bg-[var(--cf-bg)] rounded-full overflow-hidden">
        <div className="progress-glow h-full" style={{ width: `${Math.max(2, Math.min(100, currentJob.progress || 0))}%` }} />
      </div>
    </div>}
    {waitingTrend
      ? <p role="status" className="text-xs text-[var(--cf-text-2)] mt-4">{config.last_error || "Ждём свежие источники с измеренным ростом."}</p>
      : config?.last_error && <p role="alert" className="text-xs text-amber-400 mt-4">{config.last_error}</p>}
    {dailyProfiles.some((item) => item.settings?.youtube_trending) && <SourceSuggestions candidates={config?.last_run?.candidate_pool} />}
    <p className="text-[10px] text-[var(--cf-text-2)] mt-4">Для ежедневного запуска приложение и компьютер должны быть включены.</p>
  </section>;
};

const AutopilotSettings = ({ config, onUpdate }) => {
  const [form, setForm] = useState({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    if (config) setForm({ enabled: config.enabled !== false, hour: config.hour, minute: config.minute, daily_target: config.daily_target,
      max_source_videos: config.max_source_videos, continuous: config.continuous !== false, chat_analysis: config.chat_analysis !== false,
      fresh_hours: config.fresh_hours, post_interval_minutes: config.post_interval_minutes });
  }, [config]);
  const set = (key) => (e) => setForm((f) => ({ ...f, [key]: e.target.type === "checkbox" ? e.target.checked : Number(e.target.value) }));
  const save = async (e) => {
    e.preventDefault();
    if (busy || !config) return;
    setBusy(true);
    setError("");
    try { await onUpdate(form); toast.success("Настройки автопилота сохранены"); }
    catch (err) { setError(errText(err, "Не удалось сохранить настройки автопилота")); }
    finally { setBusy(false); }
  };
  const num = (key, text, min, max, hint) => <label className="text-xs">{text}
    <input data-testid={`autopilot-${key}`} className={inputClass} type="number" min={min} max={max} required value={form[key] ?? ""} onChange={set(key)} />
    {hint && <span className="text-[10px] text-[var(--cf-text-2)]">{hint}</span>}
  </label>;
  return <form className="cf-card p-5 space-y-4" onSubmit={save} data-testid="autopilot-settings">
    <h3 className="font-display font-semibold text-sm">Автопилот: поиск, нарезка и постинг</h3>
    <fieldset className="space-y-4" disabled={busy || !config}>
      <div className="flex flex-wrap gap-5 text-xs">
        <label className="flex gap-2 items-center"><input type="checkbox" checked={!!form.enabled} onChange={set("enabled")} />Автопилот включён</label>
        <label className="flex gap-2 items-center"><input data-testid="autopilot-continuous" type="checkbox" checked={!!form.continuous} onChange={set("continuous")} />Работать круглосуточно (не ждать времени запуска)</label>
        <label className="flex gap-2 items-center"><input data-testid="autopilot-chat" type="checkbox" checked={!!form.chat_analysis} onChange={set("chat_analysis")} />Учитывать всплески чата</label>
      </div>
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        {num("daily_target", "Роликов в день", 1, 30, "до 30")}
        {num("max_source_videos", "Стримов в день", 1, 15, "сколько записей разбирать")}
        {num("fresh_hours", "Свежесть, часов", 6, 96, "стрим закончился не раньше")}
        {num("post_interval_minutes", "Пауза между постами, мин", 15, 720, "для каждой площадки")}
        {!form.continuous && num("hour", "Час запуска", 0, 23)}
        {!form.continuous && num("minute", "Минуты", 0, 59)}
      </div>
      <button type="submit" data-testid="save-autopilot-btn" className="neon-btn rounded-md px-4 py-2 text-xs disabled:opacity-50">Сохранить</button>
    </fieldset>
    <p className="text-[10px] text-[var(--cf-text-2)]">Часовой пояс {config?.time_zone || "Asia/Qyzylorda"}. Сначала берутся стримы с наибольшим числом клипов зрителей и просмотров в час.</p>
    {error && <p className="text-xs text-red-400" role="alert">{error}</p>}
  </form>;
};

/* ---------------- Groq keys ---------------- */
const GroqKeysPanel = () => {
  const [keys, setKeys] = useState([]);
  const [newKey, setNewKey] = useState("");
  const [freeConfirmed, setFreeConfirmed] = useState(false);
  const [scopeId, setScopeId] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const load = useCallback(async () => {
    try {
      const { data } = await api.get("/groq/keys");
      setKeys(data);
      setError("");
    } catch (e) { setError(errText(e, "Не удалось загрузить ключи")); }
  }, []);
  useEffect(() => { load(); }, [load]);

  const add = async () => {
    const k = newKey.trim();
    if (!k || busy) return;
    setBusy(true);
    try {
      await api.post("/groq/keys", { key: k, free_tier_confirmed: freeConfirmed, scope_id: scopeId.trim() || undefined });
      toast.success("Groq ключ добавлен в пул");
      setNewKey("");
      setFreeConfirmed(false);
      await load();
    } catch (e) { toast.error(errText(e, "Ошибка добавления ключа")); }
    finally { setBusy(false); }
  };
  const remove = async (id) => {
    try { await api.delete(`/groq/keys/${id}`); await load(); }
    catch (e) { toast.error(errText(e, "Не удалось удалить ключ")); }
  };
  const confirm = async (key, checked) => {
    try {
      await api.put(`/groq/keys/${key.id}`, { free_tier_confirmed: checked });
      await load();
    } catch (e) { toast.error(errText(e, "Не удалось изменить подтверждение")); }
  };
  const updateScope = async (key, value) => {
    try { await api.put(`/groq/keys/${key.id}`, { free_tier_confirmed: !!key.free_tier_confirmed, scope_id: value.trim() }); await load(); return true; }
    catch (e) { toast.error(errText(e, "Не удалось изменить группу квоты")); return false; }
  };
  const badge = (st) =>
    st === "active" ? "text-emerald-400 border-emerald-400/40" :
    st === "cooldown" ? "text-amber-400 border-amber-400/40" : "text-red-400 border-red-400/40";
  const label = { active: "активен", cooldown: "ожидает квоту", disabled: "невалиден", unconfirmed: "тариф не подтверждён" };

  return (
    <div className="cf-card p-5 fade-up" data-testid="groq-keys-panel">
      <div className="flex items-center gap-2 mb-1">
        <KeyRound className="w-4 h-4 text-[var(--cf-cyan)]" />
        <h3 className="font-display font-semibold text-sm">Пул Groq ключей ({keys.length})</h3>
        <span className="text-[10px] rounded-full border border-emerald-400/40 text-emerald-400 px-2 py-0.5">Только бесплатные сервисы</span>
        <button className="ghost-btn rounded p-1 ml-auto" onClick={load} title="Обновить использование квоты"><RefreshCw className="w-3.5 h-3.5" /></button>
      </div>
      <p className="text-[11px] text-[var(--cf-text-2)] mb-3">
        При исчерпании квоты KudClip повторяет тот же запрос с другим доступным ключом. Если свободного лимита пока нет, задача продолжится с сохранённого этапа после сброса квоты.
      </p>
      <div className="flex gap-2">
        <input data-testid="groq-key-input" value={newKey} onChange={(e) => setNewKey(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && add()} type="password" placeholder="gsk_..."
          className="flex-1 bg-[var(--cf-bg)] border border-[var(--cf-border)] rounded-md px-3 py-2 text-sm focus:border-[var(--cf-cyan)] outline-none" />
        <button data-testid="add-groq-key-btn" onClick={add} disabled={busy} className="neon-btn rounded-md px-4 text-sm flex items-center gap-1.5 disabled:opacity-50">
          <Plus className="w-4 h-4" /> Добавить
        </button>
      </div>
      <label className="flex gap-2 items-center text-xs mt-3">
        <input data-testid="groq-free-confirmation" type="checkbox" checked={freeConfirmed} onChange={(e) => setFreeConfirmed(e.target.checked)} />
        Использовать ключ бесплатного аккаунта Groq
      </label>
      <input className={inputClass} maxLength={100} value={scopeId} onChange={(e) => setScopeId(e.target.value)} placeholder="Группа квоты / организация (необязательно)" />
      <p className="text-[10px] text-[var(--cf-text-2)] mt-1">Укажите одинаковую группу для ключей с общей квотой Groq. Пустая группа означает отдельную квоту.</p>
      {error && <div role="alert" className="text-xs text-red-400 mt-2">{error} <button className="underline" onClick={load}>Повторить</button></div>}
      {keys.length > 0 && (
        <div className="mt-3 space-y-1.5">
          {keys.map((k) => (
            <div key={k.id} data-testid={`groq-key-${k.id}`} className="flex items-center justify-between border border-[var(--cf-border)] rounded-md px-3 py-2 text-xs gap-2">
              <div className="flex items-center gap-3 min-w-0 flex-wrap">
                <span className="font-mono">{k.mask}</span>
                <span className={`border rounded-full px-2 py-0.5 text-[10px] ${badge(k.status)}`}>{label[k.status]}</span>
                <span className="text-[var(--cf-text-2)]">{Math.round((k.used_seconds || 0) / 60)} мин аудио · {k.used_requests || 0} запр. · {k.used_tokens || 0} токенов</span>
                {k.next_reset_at && <span className="text-[var(--cf-text-2)]">Сброс: {new Date(typeof k.next_reset_at === "number" ? k.next_reset_at * 1000 : k.next_reset_at).toLocaleString()}</span>}
                <label className="flex gap-1 items-center"><input type="checkbox" checked={!!k.free_tier_confirmed} onChange={(e) => confirm(k, e.target.checked)} />Бесплатный аккаунт</label>
                <label className="flex gap-1 items-center">Группа квоты<input key={`${k.id}-${k.scope_id || ""}`} defaultValue={k.scope_id || ""} maxLength={100} className="bg-[var(--cf-bg)] border border-[var(--cf-border)] rounded px-2 py-1 max-w-36" onBlur={(e) => { const element = e.currentTarget; if (element.value.trim() !== (k.scope_id || "")) updateScope(k, element.value).then((ok) => { if (!ok) element.value = k.scope_id || ""; }); }} /></label>
              </div>
              <button data-testid={`delete-groq-key-${k.id}`} onClick={() => remove(k.id)} className="text-[var(--cf-text-2)] hover:text-red-400 p-1">
                <Trash2 className="w-3.5 h-3.5" />
              </button>
            </div>
          ))}
        </div>
      )}
      {keys.length === 0 && (
        <div className="mt-3 text-[11px] text-amber-400">Добавьте ключ бесплатного аккаунта с console.groq.com/keys. Без доступной бесплатной квоты задача будет ждать.</div>
      )}
    </div>
  );
};

/* ---------------- Telegram bot ---------------- */
const TelegramPanel = () => {
  const [info, setInfo] = useState(null);
  const [token, setToken] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const load = useCallback(async () => {
    try { const { data } = await api.get("/settings"); setInfo(data); setError(""); }
    catch (e) { setError(errText(e, "Не удалось загрузить настройки бота")); }
  }, []);
  useEffect(() => { load(); }, [load]);

  const save = async () => {
    const t = token.trim();
    if (!t) return;
    setSaving(true);
    try {
      await api.put("/settings", { telegram_token: t });
      toast.success("Токен бота сохранён навсегда");
      setToken("");
      load();
    } catch (e) { toast.error(errText(e, "Ошибка сохранения токена")); }
    setSaving(false);
  };

  return (
    <div className="cf-card p-5 fade-up" data-testid="telegram-panel">
      <div className="flex items-center gap-2 mb-1">
        <Send className="w-4 h-4 text-[var(--cf-cyan)]" />
        <h3 className="font-display font-semibold text-sm">Telegram бот</h3>
        {info?.has_telegram_token && (
          <span className="border border-emerald-400/40 text-emerald-400 rounded-full px-2 py-0.5 text-[10px]" data-testid="telegram-token-status">
            подключён · {info.telegram_token_mask}
          </span>
        )}
      </div>
      <p className="text-[11px] text-[var(--cf-text-2)] mb-3">
        Токен бота от @BotFather. Сохраняется постоянно — вводить один раз. Получатели указываются в настройках профиля.
      </p>
      {error && <p role="alert" className="text-xs text-red-400 mb-2">{error} <button className="underline" onClick={load}>Повторить</button></p>}
      <div className="flex gap-2">
        <input data-testid="telegram-token-input" value={token} onChange={(e) => setToken(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && save()} type="password" placeholder="123456789:AA..."
          className="flex-1 bg-[var(--cf-bg)] border border-[var(--cf-border)] rounded-md px-3 py-2 text-sm focus:border-[var(--cf-cyan)] outline-none" />
        <button data-testid="save-telegram-token-btn" onClick={save} disabled={saving}
          className="neon-btn rounded-md px-4 text-sm flex items-center gap-1.5 disabled:opacity-50">
          {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <CheckCircle2 className="w-4 h-4" />} Сохранить
        </button>
      </div>
    </div>
  );
};

/* ---------------- Settings panel ---------------- */
const SettingsPanel = ({ profile, ads, onChange, onFlush, onAdsChanged, saveState }) => {
  const form = profile;
  const [chats, setChats] = useState([]);
  const [newChat, setNewChat] = useState("");
  const [loadingChats, setLoadingChats] = useState(false);
  useEffect(() => () => { onFlush(profile.id); }, [profile.id, onFlush]);
  const s = form.settings || {};
  const setS = (k, v) => onChange(profile.id, { settings: { [k]: v } });
  const chatIds = form.telegram_chat_ids || [];

  const addChat = (id) => {
    const v = String(id || newChat).trim();
    if (!v || chatIds.includes(v)) return;
    onChange(profile.id, { telegram_chat_ids: [...chatIds, v] });
    setNewChat("");
  };
  const removeChat = (id) => onChange(profile.id, { telegram_chat_ids: chatIds.filter((x) => x !== id) });

  const loadChats = async () => {
    setLoadingChats(true);
    try {
      const { data } = await api.get("/telegram/chats");
      setChats(data.chats);
      if (data.chats.length === 0) toast.info("Нет чатов. Получатели должны написать боту любое сообщение.");
    } catch (e) { toast.error("Не удалось получить чаты бота"); }
    setLoadingChats(false);
  };

  return (
    <details className="cf-card p-5" data-testid="settings-panel">
      <summary className="flex items-center gap-2 cursor-pointer">
        <Settings2 className="w-4 h-4 text-[var(--cf-cyan)]" />
        <span className="font-display font-semibold text-sm">Готовый пресет: 9:16 · субтитры · автоматический отбор</span>
      </summary>
      <p className="text-xs text-[var(--cf-text-2)] mt-3">Основные параметры уже выбраны. Здесь можно изменить оформление и источники доставки.</p>
      <div className="grid md:grid-cols-2 gap-5 mt-5">
        <div>
          <label className="text-xs text-[var(--cf-text-2)]" htmlFor="selection-genre">Жанр для отбора</label>
          <select id="selection-genre" data-testid="selection-genre" className={inputClass} value={s.genre || "auto"} onChange={(e) => setS("genre", e.target.value)}>
            <option value="auto">Определять автоматически</option><option value="game">Игры</option><option value="story">Истории</option><option value="education">Обучение</option><option value="humor">Юмор</option><option value="debate">Споры и обсуждения</option>
          </select>
        </div>
        <div>
          <label className="text-xs text-[var(--cf-text-2)]" htmlFor="selection-audience">Для кого</label>
          <select id="selection-audience" className={inputClass} value={s.audience || "new"} onChange={(e) => setS("audience", e.target.value)}><option value="new">Новый зритель — весь контекст внутри клипа</option><option value="fans">Знакомые с автором и темой</option></select>
        </div>
        <div className="md:col-span-2">
          <label className="text-xs text-[var(--cf-text-2)]" htmlFor="selection-preferences">Что искать и что исключать</label>
          <textarea id="selection-preferences" data-testid="selection-preferences" rows={3} maxLength={2000} className={inputClass} value={s.selection_preferences || ""} onChange={(e) => setS("selection_preferences", e.target.value)} placeholder="Например: необычные истории с развязкой; исключить политику, повторы и внутренние шутки" />
        </div>
        <div>
          <label className="text-xs text-[var(--cf-text-2)]">Порог качества: {s.quality_threshold ?? 70} / 100</label>
          <input data-testid="quality-threshold" className="w-full mt-2 accent-[var(--cf-cyan)]" type="range" min="50" max="95" step="5" value={s.quality_threshold ?? 70} onChange={(e) => setS("quality_threshold", Number(e.target.value))} />
          <p className="text-[10px] text-[var(--cf-text-2)]">Слабые моменты пропускаются. Это оценка отбора, а не гарантия популярности.</p>
        </div>
        <div className="space-y-2 text-xs">
          <label className="flex gap-2 items-center"><input data-testid="visual-analysis" type="checkbox" checked={s.visual_analysis !== false} onChange={(e) => setS("visual_analysis", e.target.checked)} />Проверять также видеоряд, когда доступна бесплатная модель</label>
        </div>
        <div className="md:col-span-2">
          <label className="text-xs text-[var(--cf-text-2)]">Получатели в Telegram (можно несколько)</label>
          <div className="flex flex-wrap gap-1.5 mt-1.5">
            {chatIds.map((id) => (
              <span key={id} data-testid={`recipient-chip-${id}`} className="flex items-center gap-1 rounded-full border border-[var(--cf-cyan)]/40 text-[var(--cf-cyan)] px-2.5 py-1 text-xs">
                {id}
                <button data-testid={`remove-recipient-${id}`} onClick={() => removeChat(id)} className="hover:text-red-400"><X className="w-3 h-3" /></button>
              </span>
            ))}
          </div>
          <div className="flex gap-2 mt-2">
            <input data-testid="telegram-chat-id-input" value={newChat} onChange={(e) => setNewChat(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && (e.preventDefault(), addChat())}
              placeholder="TG ID (например 5095885655) или @канал"
              className="flex-1 bg-[var(--cf-bg)] border border-[var(--cf-border)] rounded-md px-3 py-2 text-sm focus:border-[var(--cf-cyan)] outline-none" />
            <button data-testid="add-recipient-btn" onClick={() => addChat()} className="ghost-btn rounded-md px-3 text-xs">Добавить</button>
            <button data-testid="load-telegram-chats-btn" onClick={loadChats}
              className="ghost-btn rounded-md px-3 text-xs flex items-center gap-1.5">
              {loadingChats ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <RefreshCw className="w-3.5 h-3.5" />} Чаты бота
            </button>
          </div>
          {chats.length > 0 && (
            <div className="mt-2 flex flex-wrap gap-1.5">
              {chats.map((c) => (
                <button key={c.chat_id} data-testid={`chat-option-${c.chat_id}`} onClick={() => addChat(c.chat_id)}
                  className="ghost-btn rounded-md px-3 py-1.5 text-xs">
                  + {c.name} {c.username && `(@${c.username})`} — {c.chat_id}
                </button>
              ))}
            </div>
          )}
        </div>

        <ProfileMediaPanel settings={s} assets={ads} onAssetsChanged={onAdsChanged}
          onChange={(patch) => onChange(profile.id, { settings: patch })} />

        <div>
          <label className="text-xs text-[var(--cf-text-2)]">Шрифт субтитров</label>
          <select data-testid="font-select" value={s.font} onChange={(e) => setS("font", e.target.value)}
            className="w-full mt-1 bg-[var(--cf-bg)] border border-[var(--cf-border)] rounded-md px-3 py-2 text-sm outline-none focus:border-[var(--cf-cyan)]">
            {FONT_OPTIONS.map((f) => <option key={f.value} value={f.value}>{f.label}</option>)}
          </select>
        </div>
        <div>
          <label className="text-xs text-[var(--cf-text-2)]">Оформление ролика</label>
          <select data-testid="background-select" value={s.background || "blur"} onChange={(e) => setS("background", e.target.value)}
            className="w-full mt-1 bg-[var(--cf-bg)] border border-[var(--cf-border)] rounded-md px-3 py-2 text-sm outline-none focus:border-[var(--cf-cyan)]">
            <option value="blur">Замыленный фон + видео по центру (рекомендуется)</option>
            <option value="crop">Кроп на весь экран 9:16</option>
          </select>
          {(s.background || "blur") === "crop" && <label className="block text-xs text-[var(--cf-text-2)] mt-2">Центр кадра: {Math.round((s.crop_x ?? 0.5) * 100)}%<input data-testid="crop-x-slider" className="w-full accent-[var(--cf-cyan)]" type="range" min="0" max="1" step="0.05" value={s.crop_x ?? 0.5} onChange={(e) => setS("crop_x", Number(e.target.value))} /></label>}
        </div>
        <StyleSettings settings={s} onChange={(patch) => onChange(profile.id, { settings: patch })} />
        <div>
          <label className="text-xs text-[var(--cf-text-2)]">Насыщенность видео: {Number(s.saturation || 1.35).toFixed(2)}</label>
          <input data-testid="saturation-slider" type="range" min="1" max="1.8" step="0.05"
            value={s.saturation || 1.35} onChange={(e) => setS("saturation", parseFloat(e.target.value))}
            className="w-full mt-2 accent-[var(--cf-cyan)]" />
        </div>
        <div>
          <label className="text-xs text-[var(--cf-text-2)]">Длина клипа: от {s.clip_len_min || 20} до {s.clip_len_max || 90} сек — границы по смыслу</label>
          <div className="flex gap-3 mt-2">
            <input data-testid="clip-min-slider" type="range" min="10" max="90" step="5"
              value={s.clip_len_min || 20} onChange={(e) => onChange(profile.id, { settings: { clip_len_min: Number(e.target.value), clip_len_max: Math.max(Number(e.target.value), s.clip_len_max || 90) } })}
              className="w-full accent-[var(--cf-cyan)]" />
            <input data-testid="clip-max-slider" type="range" min="20" max="180" step="5"
              value={s.clip_len_max || 90} onChange={(e) => onChange(profile.id, { settings: { clip_len_max: Number(e.target.value), clip_len_min: Math.min(Number(e.target.value), s.clip_len_min || 20) } })}
              className="w-full accent-[var(--cf-violet)]" />
          </div>
        </div>
        <div>
          <label className="text-xs text-[var(--cf-text-2)]">Максимум клипов из одного видео: {s.clips_per_video || 3}</label>
          <input data-testid="clips-per-video-slider" type="range" min="1" max="6" step="1"
            value={s.clips_per_video || 3} onChange={(e) => setS("clips_per_video", parseInt(e.target.value))}
            className="w-full mt-2 accent-[var(--cf-cyan)]" />
        </div>
        <div>
          <label className="text-xs text-[var(--cf-text-2)]">Видео за один авто-старт: {s.auto_videos_count || 1}</label>
          <input data-testid="auto-videos-slider" type="range" min="1" max="3" step="1"
            value={s.auto_videos_count || 1} onChange={(e) => setS("auto_videos_count", parseInt(e.target.value))}
            className="w-full mt-2 accent-[var(--cf-cyan)]" />
        </div>
      </div>
      <div className="mt-4 flex items-center gap-3">
        <button data-testid="save-settings-btn" onClick={() => onFlush(profile.id)} disabled={saveState?.status === "saving"} className="neon-btn rounded-md px-5 py-2 text-sm disabled:opacity-50">
          {saveState?.status === "error" ? "Повторить сохранение" : "Сохранить настройки"}
        </button>
        <span role="status" data-testid="settings-save-status" className={`text-[11px] ${saveState?.status === "error" ? "text-red-400" : "text-[var(--cf-text-2)]"}`}>
          {saveState?.status === "saving" ? "Сохраняется…" : saveState?.status === "pending" ? "Есть несохранённые изменения…" : saveState?.status === "error" ? saveState.error : "✓ Изменения сохранены"}
        </span>
      </div>
    </details>
  );
};

/* ---------------- Channels + videos ---------------- */
/* ---------------- Jobs ---------------- */
const JobsPanel = ({ jobs, onDelete, onCancel, onRetry, busy }) => {
  if (jobs.length === 0) return null;
  const icon = (st) =>
    ["done", "review"].includes(st) ? <CheckCircle2 className="w-4 h-4 text-emerald-400" />
      : ["error", "cancelled"].includes(st) ? <XCircle className="w-4 h-4 text-red-400" />
      : <span className="pulse-dot w-2.5 h-2.5 rounded-full bg-[var(--cf-cyan)] inline-block" />;
  return (
    <div className="cf-card p-5 fade-up" data-testid="jobs-panel">
      <div className="flex items-center gap-2 mb-4">
        <Clock className="w-4 h-4 text-[var(--cf-violet)]" />
        <h3 className="font-display font-semibold text-sm">Задачи нарезки</h3>
      </div>
      <div className="space-y-3">
        {jobs.map((j) => (
          <div key={j.id} data-testid={`job-item-${j.id}`} className="border border-[var(--cf-border)] rounded-md p-3">
            <div className="flex items-center justify-between gap-3">
              <div className="flex items-center gap-2.5 min-w-0">
                {icon(j.status)}
                <div className="min-w-0">
                  <div className="text-xs font-semibold truncate">{j.video_title || j.video_url}</div>
                  <div className={`text-[11px] mt-0.5 ${j.status === "error" ? "text-red-400" : "text-[var(--cf-text-2)]"}`} data-testid={`job-status-${j.id}`}>{j.status_text || JOB_LABELS[j.status] || j.status}</div>
                  {j.status === "waiting_quota" && <div className="text-[10px] text-amber-400">Задача продолжится после восстановления бесплатной квоты.{j.next_attempt_at ? ` Следующая попытка: ${new Date(typeof j.next_attempt_at === "number" ? j.next_attempt_at * 1000 : j.next_attempt_at).toLocaleString()}` : ""}</div>}
                </div>
              </div>
              <div className="flex items-center gap-2 shrink-0">
                {["error", "cancelled"].includes(j.status) && <button data-testid={`retry-job-${j.id}`} onClick={() => onRetry(j.id)} disabled={busy === j.id} className="ghost-btn rounded px-2 py-1 text-xs disabled:opacity-50">Повторить</button>}
                {!TERMINAL_JOBS.has(j.status) && <button data-testid={`cancel-job-${j.id}`} onClick={() => onCancel(j.id)} disabled={busy === j.id} className="ghost-btn rounded px-2 py-1 text-xs disabled:opacity-50">Отменить</button>}
              <button data-testid={`delete-job-${j.id}`} onClick={() => onDelete(j.id)} disabled={busy === j.id} title="Удалить задачу" className="text-[var(--cf-text-2)] hover:text-red-400 p-1 shrink-0 disabled:opacity-50">
                <Trash2 className="w-3.5 h-3.5" />
              </button>
              </div>
            </div>
            {!TERMINAL_JOBS.has(j.status) && (
              <div className="h-1.5 rounded-full bg-[var(--cf-bg)] mt-2.5 overflow-hidden">
                <div className="progress-glow h-full rounded-full" style={{ width: `${Math.max(0, Math.min(100, j.progress || 0))}%` }} />
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  );
};

/* ---------------- Ready publication packages ---------------- */
const PublicationCard = ({ clip, timeZone }) => {
  const caption = clip.publication_caption || clip.caption || clip.title || "";
  const copyCaption = async () => {
    try { await navigator.clipboard.writeText(caption); toast.success("Подпись скопирована — можно публиковать"); }
    catch { toast.error("Не удалось скопировать подпись"); }
  };
  return <article className="cf-card overflow-hidden" data-testid={`publication-card-${clip.id}`}>
    <div className="p-5 md:p-6 flex justify-between items-start gap-3 flex-wrap">
      <div className="space-y-2">
        <p className="text-[10px] uppercase tracking-widest text-emerald-400">Готово к публикации</p>
        <h3 className="font-display font-semibold text-lg">{clip.title}</h3>
        {clip.created_at && <p className="text-[11px] text-[var(--cf-text-2)]">{formatTimestamp(clip.created_at, timeZone)}</p>}
        <SourceDetails item={clip} timeZone={timeZone} />
      </div>
      {clip.package_filename && <a data-testid={`download-package-${clip.id}`} href={`${API}/clips/${clip.id}/package`} download
        className="neon-btn rounded-lg px-4 py-2.5 text-xs inline-flex items-center gap-2"><Archive className="w-4 h-4" />Скачать комплект</a>}
    </div>
    <div className="px-5 pb-5 md:px-6 md:pb-6 grid md:grid-cols-[200px_1fr] gap-6">
      <div><video data-testid={`publication-video-${clip.id}`} className="publication-video rounded-lg w-full aspect-[9/16] bg-black object-contain" controls preload="metadata" src={`${API}/clips/${clip.id}/file`} poster={clip.cover_filename ? `${API}/clips/${clip.id}/cover` : undefined} /><a className="ghost-btn rounded-md py-2 px-3 mt-3 text-xs inline-flex items-center justify-center gap-2 w-full" href={`${API}/clips/${clip.id}/file`} download><Download className="w-3.5 h-3.5" />Скачать видео</a></div>
      <div className="space-y-4 min-w-0"><div className="grid sm:grid-cols-2 gap-4">{clip.cover_filename && <figure><img data-testid={`publication-cover-${clip.id}`} className="w-full aspect-video object-cover bg-black rounded-lg" src={`${API}/clips/${clip.id}/cover`} alt={`Обложка: ${clip.title}`} loading="lazy" /><figcaption className="text-xs text-[var(--cf-text-2)] mt-2 flex justify-between">Обложка<a className="text-[var(--cf-cyan)]" href={`${API}/clips/${clip.id}/cover`} download>Скачать</a></figcaption></figure>}{clip.banner_filename && <figure><img data-testid={`publication-banner-${clip.id}`} className="w-full aspect-video object-contain bg-black/30 rounded-lg" src={`${API}/clips/${clip.id}/banner`} alt={clip.banner_text || "Баннер для публикации"} loading="lazy" /><figcaption className="text-xs text-[var(--cf-text-2)] mt-2 flex justify-between">Баннер<a className="text-[var(--cf-cyan)]" href={`${API}/clips/${clip.id}/banner`} download>Скачать</a></figcaption></figure>}</div>
        {clip.banner_text && <p className="text-xs text-[var(--cf-text-2)]">{clip.banner_text}</p>}
        <div className="border border-[var(--cf-border)] rounded-lg p-4"><div className="flex justify-between items-center gap-3"><h4 className="text-xs font-semibold">Подпись для публикации</h4><button data-testid={`copy-caption-${clip.id}`} onClick={copyCaption} className="ghost-btn rounded px-3 py-1.5 text-xs inline-flex items-center gap-1.5"><Copy className="w-3.5 h-3.5" />Копировать</button></div><p data-testid={`publication-caption-${clip.id}`} className="text-sm text-[var(--cf-text-2)] whitespace-pre-wrap break-words mt-3">{caption}</p></div>
        {clip.sent_to_telegram && <p className="text-[11px] text-emerald-400 flex items-center gap-1.5"><CheckCircle2 className="w-3.5 h-3.5" />Ролик отправлен в Telegram</p>}
      </div>
    </div>
  </article>;
};
const PublicationsGallery = ({ clips, timeZone }) => {
  const ready = clips.filter((clip) => clip.ready_for_publish);
  return <section className="space-y-4" data-testid="publications-gallery"><div className="flex items-center justify-between gap-3"><h2 className="font-display font-semibold text-lg">Готово к публикации</h2><span className="text-xs text-[var(--cf-text-2)]">{ready.length} {ready.length === 1 ? "комплект" : ready.length > 1 && ready.length < 5 ? "комплекта" : "комплектов"}</span></div>{ready.length ? ready.map((clip) => <PublicationCard key={clip.id} clip={clip} timeZone={timeZone} />) : <div className="cf-card p-8 md:p-12 text-center"><Play className="w-7 h-7 text-[var(--cf-cyan)] mx-auto mb-4" /><p className="text-sm font-semibold">Здесь появятся готовые выпуски</p><p className="text-xs text-[var(--cf-text-2)] max-w-md mx-auto mt-2">У каждого выпуска будут видео, обложка и подпись.</p></div>}</section>;
};

/* ---------------- App ---------------- */
export default function App() {
  const [profiles, setProfiles] = useState([]);
  const [activeId, setActiveId] = useState(null);
  const [jobs, setJobs] = useState([]);
  const [clips, setClips] = useState([]);
  const [ads, setAds] = useState([]);
  const [jobBusy, setJobBusy] = useState(null);
  const [autopilot, setAutopilot] = useState(null);
  const [autopilotBusy, setAutopilotBusy] = useState(false);
  const [initialLoading, setInitialLoading] = useState(true);
  const [initialError, setInitialError] = useState("");
  const [dataError, setDataError] = useState("");
  const [saveStates, setSaveStates] = useState({});
  const profilesRef = useRef(profiles);
  const activeRef = useRef(activeId);
  const requestsRef = useRef({ id: 0, controller: null });
  const initialRef = useRef({ id: 0, controller: null });
  const adsRequestId = useRef(0);
  const saveQueues = useRef(new Map());
  profilesRef.current = profiles;
  activeRef.current = activeId;
  const profile = profiles.find((p) => p.id === activeId);
  const hasActiveJobs = jobs.some((job) => !TERMINAL_JOBS.has(job.status));
  const fastRefresh = hasActiveJobs || autopilotBusy || ["running", "selecting", "enqueuing", "searching", "preparing"].includes(autopilot?.status);

  // Every profile owns one serial PATCH queue. Newer edits overlay older responses.
  const flushProfile = useCallback((id) => {
    const entry = saveQueues.current.get(id);
    if (!entry || entry.deleted) return Promise.resolve(true);
    clearTimeout(entry.timer);
    if (entry.running) return entry.running;
    if (!Object.keys(entry.pending).length) return Promise.resolve(true);
    entry.running = (async () => {
      setSaveStates((prev) => ({ ...prev, [id]: { status: "saving" } }));
      while (Object.keys(entry.pending).length && !entry.deleted) {
        const payload = entry.pending;
        entry.pending = {};
        entry.inflight = payload;
        try {
          const { data } = await api.patch(`/profiles/${id}`, payload);
          entry.inflight = {};
          const newerEdits = entry.pending;
          if (!entry.deleted) setProfiles((prev) => prev.map((p) => p.id === id ? mergePatch(data, newerEdits) : p));
        } catch (e) {
          entry.pending = mergePatch(payload, entry.pending);
          entry.inflight = {};
          if (!entry.deleted) {
            const error = errText(e, "Не удалось сохранить. Изменения сохранены в открытом приложении.");
            setSaveStates((prev) => ({ ...prev, [id]: { status: "error", error } }));
            toast.error(`Настройки не сохранены: ${error}`);
          }
          return false;
        }
      }
      if (!entry.deleted) setSaveStates((prev) => ({ ...prev, [id]: { status: "saved" } }));
      return true;
    })().finally(() => { entry.running = null; });
    return entry.running;
  }, []);

  const changeProfile = useCallback((id, patch) => {
    let entry = saveQueues.current.get(id);
    if (!entry) { entry = { pending: {}, inflight: {}, timer: null, running: null, deleted: false }; saveQueues.current.set(id, entry); }
    if (entry.deleted) return;
    entry.pending = mergePatch(entry.pending, patch);
    setProfiles((prev) => prev.map((p) => p.id === id ? mergePatch(p, patch) : p));
    setSaveStates((prev) => ({ ...prev, [id]: { status: entry.running ? "saving" : "pending" } }));
    clearTimeout(entry.timer);
    entry.timer = setTimeout(() => { flushProfile(id); }, 700);
  }, [flushProfile]);

  const selectProfile = useCallback((id) => {
    if (activeRef.current) flushProfile(activeRef.current);
    activeRef.current = id;
    setActiveId(id);
  }, [flushProfile]);

  const loadInitial = useCallback(async () => {
    initialRef.current.controller?.abort();
    const controller = new AbortController();
    const requestId = ++initialRef.current.id;
    initialRef.current.controller = controller;
    setInitialLoading(true); setInitialError("");
    try {
      const [p, a] = await Promise.all([api.get("/profiles", { signal: controller.signal }), api.get("/ads", { signal: controller.signal })]);
      if (requestId !== initialRef.current.id) return;
      setProfiles(p.data.map((item) => {
        const entry = saveQueues.current.get(item.id);
        return entry ? mergePatch(mergePatch(item, entry.inflight), entry.pending) : item;
      }));
      setAds(a.data);
      setActiveId((cur) => cur && p.data.some((item) => item.id === cur) ? cur : (p.data[0]?.id || null));
    } catch (e) {
      if (!controller.signal.aborted && requestId === initialRef.current.id) setInitialError(errText(e, "Не удалось подключиться к серверу. Проверьте, что backend запущен."));
    } finally { if (requestId === initialRef.current.id) setInitialLoading(false); }
  }, []);

  const loadAds = useCallback(async () => {
    const requestId = ++adsRequestId.current;
    try {
      const { data } = await api.get("/ads");
      if (requestId === adsRequestId.current) setAds(data);
    } catch (e) {
      if (requestId === adsRequestId.current) toast.error(errText(e, "Не удалось обновить список медиафайлов"));
    }
  }, []);

  const loadJobsClips = useCallback(async () => {
    requestsRef.current.controller?.abort();
    const controller = new AbortController();
    const requestId = ++requestsRef.current.id;
    requestsRef.current.controller = controller;
    try {
      const [j, c, a] = await Promise.all([
        api.get("/jobs", { signal: controller.signal }),
        api.get("/clips", { signal: controller.signal }),
        api.get("/autopilot", { signal: controller.signal }),
      ]);
      if (requestId !== requestsRef.current.id) return;
      setJobs(j.data); setClips(c.data); setAutopilot(a.data); setDataError("");
    } catch (e) {
      if (!controller.signal.aborted && requestId === requestsRef.current.id) setDataError(errText(e, "Не удалось обновить автопилот и готовые выпуски"));
    }
  }, []);

  useEffect(() => {
    const initialRequest = initialRef.current;
    loadInitial();
    return () => { initialRequest.id += 1; initialRequest.controller?.abort(); };
  }, [loadInitial]);

  useEffect(() => {
    const queues = saveQueues.current;
    return () => { for (const id of queues.keys()) flushProfile(id); };
  }, [flushProfile]);

  useEffect(() => {
    const profileRequest = requestsRef.current;
    loadJobsClips();
    return () => { profileRequest.id += 1; profileRequest.controller?.abort(); };
  }, [loadJobsClips]);

  useEffect(() => {
    let stopped = false, timer;
    const delay = fastRefresh ? 3000 : 30000;
    const tick = async () => {
      await loadJobsClips();
      if (!stopped) timer = setTimeout(tick, delay);
    };
    timer = setTimeout(tick, delay);
    return () => { stopped = true; clearTimeout(timer); };
  }, [fastRefresh, loadJobsClips]);

  const createProfile = async (name) => {
    try {
      const { data } = await api.post("/profiles", { name });
      setProfiles((prev) => [data, ...prev.filter((p) => p.id !== data.id)]);
      selectProfile(data.id);
      toast.success(`Профиль «${name}» создан`);
    } catch (e) { toast.error(errText(e, "Не удалось создать профиль")); }
  };

  const deleteProfile = async (id) => {
    await flushProfile(id);
    try {
      await api.delete(`/profiles/${id}`);
      const entry = saveQueues.current.get(id);
      if (entry) { entry.deleted = true; clearTimeout(entry.timer); }
      setProfiles((prev) => prev.filter((p) => p.id !== id));
      if (activeRef.current === id) selectProfile(profilesRef.current.find((p) => p.id !== id)?.id || null);
      toast.success("Профиль удалён");
    } catch (e) { toast.error(errText(e, "Не удалось удалить профиль")); }
  };

  const saveSources = async (profileId, patch) => {
    changeProfile(profileId, patch);
    return flushProfile(profileId);
  };

  const runAutopilot = async () => {
    if (autopilotBusy) return;
    setAutopilotBusy(true);
    try {
      const saved = await Promise.all(profilesRef.current.map((item) => flushProfile(item.id)));
      if (saved.some((ok) => !ok)) return;
      const { data } = await api.post("/autopilot/run");
      if (data.error || data.last_error) toast.warning(data.error || data.last_error);
      else if (data.already_ready || ["ready", "done"].includes(data.status)) toast.info("Готовые выпуски уже в галерее");
      else toast.success("Автопилот готовит выпуски. Комплекты появятся в галерее.");
      await loadJobsClips();
    } catch (error) { toast.error(errText(error, "Не удалось запустить автопилот")); }
    finally { setAutopilotBusy(false); }
  };
  const updateAutopilot = async (patch) => {
    const { data } = await api.put("/autopilot", patch);
    requestsRef.current.controller?.abort();
    requestsRef.current.id += 1;
    setAutopilot(data);
    toast.success("Настройки автопилота сохранены");
  };

  const jobAction = async (jobId, action) => {
    setJobBusy(jobId);
    try {
      if (action === "delete") await api.delete(`/jobs/${jobId}`);
      else await api.post(`/jobs/${jobId}/${action}`);
      await loadJobsClips();
    } catch (e) { toast.error(errText(e, "Не удалось обновить задачу")); }
    finally { setJobBusy(null); }
  };
  return (
    <div className="min-h-screen text-[var(--cf-text)]">
      <div className="bg-video-wrap" aria-hidden="true"><video className="bg-video" src="/background.mp4" autoPlay loop muted playsInline /><div className="bg-video-overlay" /></div>
      <Toaster theme="dark" position="top-right" richColors />
      <main className="relative z-10 max-w-[1120px] mx-auto p-5 md:p-8 space-y-6 md:space-y-8">
        <header className="pt-3 md:pt-6"><div className="flex items-center justify-between gap-4 mb-7"><div className="flex items-center gap-2.5"><img src="/logo.jpg" alt="" className="w-9 h-9 rounded-lg" /><span className="font-display font-extrabold text-sm tracking-wider">KUDCLIP</span></div><span className="text-[10px] text-emerald-400 border border-emerald-400/30 rounded-full px-3 py-1">Без платных сервисов</span></div><h1 className="font-display font-extrabold text-2xl md:text-4xl leading-tight">Готовые ролики каждый день</h1><p className="text-sm text-[var(--cf-text-2)] mt-4">Автоматический отбор, монтаж и публикация</p></header>
        {(initialLoading && !autopilot) && <div role="status" className="text-sm flex gap-2 items-center text-[var(--cf-text-2)]"><Loader2 className="w-4 h-4 animate-spin" />Подключаюсь к серверу…</div>}
        {(initialError || dataError) && <div role="alert" className="cf-card p-4 text-sm text-red-400">{dataError || initialError}<button data-testid="retry-connection" className="ghost-btn rounded px-3 py-1 ml-3" onClick={() => { loadInitial(); loadJobsClips(); }}>Повторить подключение</button></div>}
        <AuthorsPage profiles={profiles} setProfiles={setProfiles} />
        <PublicationsGallery clips={clips} timeZone={autopilot?.time_zone} />
        <details data-testid="advanced-settings" className="border-t border-[var(--cf-border)] pt-5"><summary className="cursor-pointer text-sm text-[var(--cf-text-2)] flex items-center gap-2"><Settings2 className="w-4 h-4" />Настройки</summary><div className="space-y-4 mt-5">
          <details className="cf-card p-5"><summary className="cursor-pointer text-sm font-semibold">Старый общий автопилот (работает, только если выключен ежедневный запуск авторов)</summary><div className="mt-4 space-y-4">
            <AutopilotPanel config={autopilot} onRun={runAutopilot} busy={autopilotBusy} profiles={profiles}
              activeJobs={jobs.filter((job) => !TERMINAL_JOBS.has(job.status))} />
            <AutopilotSettings config={autopilot} onUpdate={updateAutopilot} />
            <BrowserPostingPanel />
          </div></details>
          {Object.entries(saveStates).filter(([id, state]) => state.status === "error" && profiles.some((item) => item.id === id)).map(([id, state]) => <div key={id} role="alert" className="cf-card p-3 text-xs text-red-400">Не сохранён профиль «{profiles.find((item) => item.id === id)?.name}»: {state.error} <button className="underline ml-2" onClick={() => flushProfile(id)}>Повторить</button></div>)}
          <ProfileManager profiles={profiles} activeId={activeId} onSelect={selectProfile} onCreate={createProfile} onDelete={deleteProfile} />
          {profile && <>
            <ProfileSources key={`channels-${profile.id}`} profile={profile} onSave={(patch) => saveSources(profile.id, patch)} />
            <SettingsPanel key={`settings-${profile.id}`} profile={profile} ads={ads} onChange={changeProfile}
              onFlush={flushProfile} onAdsChanged={loadAds} saveState={saveStates[profile.id]} />
          </>}
          <details className="cf-card p-5"><summary className="cursor-pointer text-sm font-semibold">Telegram — дополнительная доставка</summary><div className="mt-4"><TelegramPanel /></div></details>
          <details className="cf-card p-5" open><summary className="cursor-pointer text-sm font-semibold">ИИ и распознавание речи</summary><div className="mt-4"><AiSettingsPanel /></div></details>
          <details className="cf-card p-5"><summary className="cursor-pointer text-sm font-semibold">Ключи бесплатных аккаунтов Groq</summary><div className="mt-4"><GroqKeysPanel /></div></details>
          <details className="cf-card p-5"><summary className="cursor-pointer text-sm font-semibold">История обработки</summary><div className="mt-4"><JobsPanel jobs={jobs} busy={jobBusy} onDelete={(id) => jobAction(id, "delete")} onCancel={(id) => jobAction(id, "cancel")} onRetry={(id) => jobAction(id, "retry")} /></div></details>
          <a className="text-xs text-[var(--cf-text-2)] underline inline-block" href={`${API}/source/archive`}>Скачать исходники приложения</a>
        </div></details>
      </main>
    </div>
  );
}
