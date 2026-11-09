import { useState } from "react";
import { Link2, Twitch, X, Youtube } from "lucide-react";

const twitchPresets = [
  "Sasavot", "5opka", "rostislav_999", "rostikfacekid", "bratishkinoff", "strogo",
  "buster", "evelone192", "paradeev1ch", "t2x2", "voodoosh",
];
const sourceTypes = {
  youtube: {
    name: "YouTube", icon: Youtube, inputId: "channel-url-input", addId: "add-channel-btn",
    placeholder: "https://www.youtube.com/@канал",
    description: "Поиск среди видео, опубликованных за последние 48 часов.",
  },
  twitch: {
    name: "Twitch", icon: Twitch, inputId: "twitch-channel-url-input", addId: "add-twitch-channel-btn",
    placeholder: "https://www.twitch.tv/канал",
    description: "Каналы, из которых можно брать популярные русскоязычные клипы.",
  },
};
const buttonClass = "ghost-btn rounded-md px-3 py-2 text-xs disabled:opacity-50";
const inputClass = "flex-1 min-w-0 bg-[var(--cf-bg)] border border-[var(--cf-border)] rounded-md px-3 py-2 text-sm";

function twitchChannelUrl(value) {
  try {
    const url = new URL(value.trim());
    const match = url.pathname.match(/^\/([a-z0-9_]+)\/?$/i);
    if (url.protocol === "https:" && ["www.twitch.tv", "twitch.tv", "m.twitch.tv"].includes(url.hostname)
        && !url.username && !url.password && !url.port && match) {
      return `https://www.twitch.tv/${match[1].toLowerCase()}`;
    }
  } catch { /* Input may be an incomplete link. */ }
  return "";
}

function SourceList({ type, channels, onSave }) {
  const [url, setUrl] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const { name, icon: Icon, inputId, addId, placeholder, description } = sourceTypes[type];
  const normalize = type === "twitch" ? twitchChannelUrl : (value) => value.trim();
  const existing = new Set(channels.map(normalize));

  const saveChannels = async (next) => {
    if (busy) return false;
    setBusy(true);
    try { return await onSave(next); }
    finally { setBusy(false); }
  };

  const addChannel = async (candidate) => {
    const normalized = normalize(candidate);
    if (!normalized) {
      setError(`Укажите ссылку на канал ${name}.`);
      return false;
    }
    if (existing.has(normalized)) {
      setError("Этот канал уже есть в списке.");
      return false;
    }
    setError("");
    return saveChannels([...channels, normalized]);
  };

  return <section className="space-y-3 min-w-0" aria-label={`${name} источники`}>
    <h4 className="text-sm font-semibold flex items-center gap-2"><Icon className="w-4 h-4 text-[var(--cf-cyan)]" />{name}</h4>
    <p className="text-xs text-[var(--cf-text-2)]">{description}</p>
    <div className="flex gap-2 flex-wrap">
      {channels.map((channel) => <div key={channel} className="max-w-full text-xs border border-[var(--cf-border)] rounded-full px-3 py-1.5 flex items-center gap-2">
        <Link2 className="w-3 h-3 shrink-0" />
        <span className="break-all">{channel.replace(/^https?:\/\/(?:www\.)?(?:youtube|twitch)\.(?:com|tv)\//i, "")}</span>
        <button type="button" disabled={busy} aria-label={`Удалить канал ${channel}`} className="shrink-0 hover:text-red-400"
          onClick={() => { setError(""); saveChannels(channels.filter((item) => item !== channel)); }}><X className="w-3 h-3" /></button>
      </div>)}
    </div>
    <form className="flex gap-2" onSubmit={async (event) => {
      event.preventDefault();
      if (await addChannel(url)) setUrl("");
    }}>
      <input data-testid={inputId} aria-label={`Ссылка на канал ${name}`} value={url} disabled={busy}
        onChange={(event) => { setUrl(event.target.value); setError(""); }} className={inputClass} placeholder={placeholder} />
      <button data-testid={addId} type="submit" disabled={busy} className={buttonClass}>Добавить</button>
    </form>
    {type === "twitch" && <details className="text-xs text-[var(--cf-text-2)]">
      <summary className="cursor-pointer">Рекомендуемые каналы</summary>
      <p className="mt-2">Добавьте нужные каналы по одному.</p>
      <div className="flex gap-2 flex-wrap mt-2">
        {twitchPresets.map((login) => {
          const channel = `https://www.twitch.tv/${login.toLowerCase()}`;
          const added = existing.has(channel);
          return <button key={login} type="button" data-testid={`twitch-preset-${login.toLowerCase()}`}
            aria-label={added ? `${login} уже добавлен` : `Добавить ${login}`}
            disabled={busy || added} className={buttonClass} onClick={() => addChannel(channel)}>{added ? "✓ " : "+ "}{login}</button>;
        })}
      </div>
    </details>}
    {error && <p className="text-xs text-red-400" role="alert">{error}</p>}
  </section>;
}

export default function ProfileSources({ profile, onSave }) {
  const [saving, setSaving] = useState(false);
  const twitchChannels = profile.twitch_channels || [];
  const youtubeEnabled = Boolean(profile.settings?.youtube_trending);
  const twitchEnabled = Boolean(profile.settings?.twitch_trending);
  const saveTrending = async (setting, enabled) => {
    setSaving(true);
    try { await onSave({ settings: { [setting]: enabled } }); }
    finally { setSaving(false); }
  };
  return <div className="cf-card p-5 space-y-4" data-testid="channel-browser">
    <h3 className="font-display font-semibold text-sm">Источники автопилота</h3>
    <div className="grid md:grid-cols-2 gap-5">
      <div className="space-y-3 min-w-0">
        <SourceList type="youtube" channels={profile.channels || []} onSave={(channels) => onSave({ channels })} />
        <label className="flex items-center gap-2 text-xs">
          <input type="checkbox" data-testid="youtube-trending" checked={youtubeEnabled} disabled={saving}
            onChange={(event) => saveTrending("youtube_trending", event.target.checked)} />
          Показывать свежие видео популярных русскоязычных авторов
        </label>
      </div>
      <div className="space-y-3 min-w-0">
        <SourceList type="twitch" channels={twitchChannels} onSave={(channels) => onSave({ twitch_channels: channels })} />
        <label className="flex items-center gap-2 text-xs">
          <input type="checkbox" data-testid="twitch-trending" checked={twitchEnabled}
            disabled={saving || (!twitchChannels.length && !twitchEnabled)}
            onChange={(event) => saveTrending("twitch_trending", event.target.checked)} />
          Искать популярные русскоязычные клипы Twitch
        </label>
        {!twitchChannels.length && <p className="text-xs text-[var(--cf-text-2)]">Добавьте канал, чтобы включить поиск.</p>}
      </div>
    </div>
  </div>;
}
