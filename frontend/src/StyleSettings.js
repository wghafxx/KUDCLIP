import { fontFamily } from "@/lib/api";

const field = "w-full mt-1 bg-[var(--cf-bg)] border border-[var(--cf-border)] rounded-md px-3 py-2 text-sm outline-none focus:border-[var(--cf-cyan)]";
const label = "text-xs text-[var(--cf-text-2)]";

const Toggle = ({ id, checked, onChange, children }) => (
  <label className="flex items-center gap-2 text-xs cursor-pointer">
    <input data-testid={id} type="checkbox" checked={checked} onChange={(e) => onChange(e.target.checked)} />
    {children}
  </label>
);

const Range = ({ id, text, min, max, step, value, onChange }) => (
  <label className={`${label} block`}>{text}
    <input data-testid={id} className="w-full mt-2 accent-[var(--cf-cyan)]" type="range" min={min} max={max} step={step}
      value={value} onChange={(e) => onChange(Number(e.target.value))} />
  </label>
);

const Preview = ({ s }) => {
  const glow = s.sub_glow ? `0 0 14px ${s.glow_color || "#06B6D4"}, 0 0 28px ${s.glow_color || "#06B6D4"}` : "";
  const outline = `${(s.sub_outline ?? 4) / 3}px`;
  const align = { top: "flex-start", middle: "center", bottom: "flex-end" }[s.sub_position || "bottom"];
  const bannerAlign = { top: "top-[9%]", middle: "top-1/2 -translate-y-1/2", bottom: "bottom-[9%]" }[s.banner_position || "top"];
  const text = ["Он реально", "это сделал?!", "смотри до конца"].join(" ").split(" ").slice(0, s.sub_words ?? 3).join(" ");
  return <div className="mx-auto w-44 h-80 rounded-xl border border-[var(--cf-border)] bg-gradient-to-b from-slate-800 to-slate-950 relative overflow-hidden flex flex-col p-3"
    style={{ justifyContent: align }} data-testid="style-preview">
    {s.ad_enabled && <div className={`absolute left-1/2 -translate-x-1/2 ${bannerAlign} bg-amber-400/90 text-black text-[9px] font-bold rounded px-2 py-1 text-center`}
      style={{ width: `${Math.round((s.banner_scale ?? 0.67) * 100)}%` }}>БАННЕР</div>}
    <p className="text-center font-black leading-tight" style={{
      fontFamily: fontFamily(), fontStyle: s.font === "montserrat_italic" || !s.font ? "italic" : "normal",
      fontSize: `${Math.round((s.sub_size ?? 55) / 5)}px`, color: s.sub_color || "#FFFFFF",
      textTransform: s.sub_uppercase ? "uppercase" : "none",
      WebkitTextStroke: `${outline} black`, paintOrder: "stroke fill", textShadow: glow,
    }}>{text}</p>
  </div>;
};

export default function StyleSettings({ settings: s, onChange }) {
  const set = (key) => (value) => onChange({ [key]: value });
  return <div className="md:col-span-2 grid md:grid-cols-[1fr_auto] gap-6 border border-[var(--cf-border)] rounded-lg p-4" data-testid="style-settings">
    <div className="grid sm:grid-cols-2 gap-4">
      <h4 className="sm:col-span-2 text-sm font-semibold">Субтитры</h4>
      <Toggle id="sub-glow-toggle" checked={Boolean(s.sub_glow)} onChange={set("sub_glow")}>Свечение вокруг текста</Toggle>
      <Toggle id="sub-uppercase-toggle" checked={Boolean(s.sub_uppercase)} onChange={set("sub_uppercase")}>ЗАГЛАВНЫЕ БУКВЫ</Toggle>
      <Toggle id="sub-animate-toggle" checked={Boolean(s.sub_animate)} onChange={set("sub_animate")}>Анимация появления слов</Toggle>
      <div className="flex gap-4">
        <label className={label}>Цвет текста<input data-testid="sub-color-input" type="color" className="block w-10 h-8 mt-1 rounded" value={s.sub_color || "#FFFFFF"} onChange={(e) => set("sub_color")(e.target.value.toUpperCase())} /></label>
        <label className={`${label} ${s.sub_glow ? "" : "opacity-40"}`}>Цвет свечения<input data-testid="glow-color-input" type="color" disabled={!s.sub_glow} className="block w-10 h-8 mt-1 rounded" value={s.glow_color || "#06B6D4"} onChange={(e) => set("glow_color")(e.target.value.toUpperCase())} /></label>
      </div>
      <Range id="sub-size-slider" text={`Размер: ${s.sub_size ?? 55}`} min={40} max={140} step={5} value={s.sub_size ?? 55} onChange={set("sub_size")} />
      <Range id="sub-words-slider" text={`Слов на экране: ${s.sub_words ?? 3}`} min={1} max={6} step={1} value={s.sub_words ?? 3} onChange={set("sub_words")} />
      <Range id="sub-outline-slider" text={`Толщина обводки: ${s.sub_outline ?? 4}`} min={0} max={12} step={1} value={s.sub_outline ?? 4} onChange={set("sub_outline")} />
      <label className={label}>Где показывать
        <select data-testid="sub-position-select" className={field} value={s.sub_position || "bottom"} onChange={(e) => set("sub_position")(e.target.value)}>
          <option value="bottom">Внизу</option><option value="middle">По центру</option><option value="top">Вверху</option>
        </select>
      </label>
      <h4 className="sm:col-span-2 text-sm font-semibold mt-2">Рекламный баннер</h4>
      <p className="sm:col-span-2 text-[10px] text-[var(--cf-text-2)] -mt-3">Картинку выбери выше в блоке рекламы. Здесь — где и как долго она видна.</p>
      <label className={label}>Положение
        <select data-testid="banner-position-select" className={field} value={s.banner_position || "top"} onChange={(e) => set("banner_position")(e.target.value)}>
          <option value="top">Сверху</option><option value="middle">По центру</option><option value="bottom">Снизу</option>
        </select>
      </label>
      <label className={label}>Показ
        <select data-testid="banner-mode-select" className={field} value={s.banner_mode || "timed"} onChange={(e) => set("banner_mode")(e.target.value)}>
          <option value="timed">На несколько секунд</option><option value="always">Весь ролик</option>
        </select>
      </label>
      <Range id="banner-scale-slider" text={`Ширина баннера: ${Math.round((s.banner_scale ?? 0.67) * 100)}%`} min={0.3} max={1} step={0.05} value={s.banner_scale ?? 0.67} onChange={set("banner_scale")} />
      {(s.banner_mode || "timed") === "timed" && <Range id="banner-seconds-slider" text={`Секунд на экране: ${s.banner_seconds ?? 3.5}`} min={1} max={15} step={0.5} value={s.banner_seconds ?? 3.5} onChange={set("banner_seconds")} />}
    </div>
    <Preview s={s} />
  </div>;
}
