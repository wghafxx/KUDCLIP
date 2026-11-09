import { useRef, useState } from "react";
import { Loader2, Megaphone, Plus, Trash2 } from "lucide-react";
import { toast } from "sonner";
import { API, api } from "@/lib/api";

const assetTypes = {
  video: { label: "Загрузить видеовставку", accept: "video/*", testId: "ad-file-input" },
  banner: { label: "Загрузить баннер", accept: "image/png,image/jpeg,image/webp,.png,.jpg,.jpeg,.webp", testId: "banner-file-input" },
  loop: { label: "Загрузить нижний ролик", accept: "video/mp4,.mp4", testId: "loop-file-input" },
};
const buttonClass = "ghost-btn rounded-md px-3 py-2 text-xs inline-flex items-center gap-2 disabled:opacity-50";
const fieldClass = "w-full mt-1 bg-[var(--cf-bg)] border border-[var(--cf-border)] rounded-md px-3 py-2 text-sm disabled:opacity-50";

function UploadAsset({ kind, onSelect, busy }) {
  const input = useRef(null);
  const { label, accept, testId } = assetTypes[kind];
  return <>
    <input ref={input} data-testid={testId} type="file" accept={accept} hidden disabled={Boolean(busy)} onChange={(event) => {
      const file = event.target.files?.[0];
      event.target.value = "";
      if (file) onSelect(kind, file);
    }} />
    <button type="button" className={buttonClass} disabled={Boolean(busy)} onClick={() => input.current?.click()}>
      {busy === kind ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Plus className="w-3.5 h-3.5" />}
      {label}
    </button>
  </>;
}

export default function ProfileMediaPanel({ settings, assets, onChange, onAssetsChanged }) {
  const [adName, setAdName] = useState("");
  const [pending, setPending] = useState("");
  const ads = assets.filter((asset) => !asset.kind || asset.kind === "video" || asset.kind === "banner");
  const loops = assets.filter((asset) => asset.kind === "loop");
  const selectedAd = ads.find((asset) => asset.id === settings.ad_id);
  const selectedLoop = loops.find((asset) => asset.id === settings.retention_loop_id);
  const split = settings.retention_style === "split";

  const uploadAsset = async (kind, file) => {
    if (pending) return;
    setPending(kind);
    const form = new FormData();
    const name = kind === "loop" ? file.name : adName.trim() || file.name;
    form.append("file", file);
    form.append("name", name.slice(0, 100));
    form.append("kind", kind);
    try {
      const { data } = await api.post("/ads", form);
      if (kind === "loop") {
        onChange({ retention_loop_id: data.id });
      } else {
        onChange({ ad_id: data.id, ad_enabled: true });
        setAdName("");
      }
      await onAssetsChanged();
      toast.success("Файл загружен и выбран");
    } catch (error) {
      const detail = error?.response?.data?.detail;
      toast.error(typeof detail === "string" ? detail : "Не удалось загрузить файл");
    } finally {
      setPending("");
    }
  };

  const deleteAsset = async (asset) => {
    if (pending) return;
    setPending(`delete-${asset.id}`);
    try {
      await api.delete(`/ads/${asset.id}`);
      if (settings.ad_id === asset.id) onChange({ ad_id: "", ad_enabled: false });
      if (settings.retention_loop_id === asset.id) onChange({ retention_loop_id: "" });
      await onAssetsChanged();
    } catch (error) {
      const detail = error?.response?.data?.detail;
      toast.error(typeof detail === "string" ? detail : "Не удалось удалить файл");
    } finally {
      setPending("");
    }
  };

  return <div className="md:col-span-2 space-y-4">
    <section className="border border-[var(--cf-border)] rounded-md p-4 space-y-3" aria-label="Реклама в новых роликах">
      <h4 className="flex items-center gap-2 text-sm font-semibold">
        <Megaphone className="w-4 h-4 text-[var(--cf-pink)]" />Реклама в новых роликах
      </h4>
      <p className="text-xs text-[var(--cf-text-2)]">
        Для профиля можно выбрать один рекламный файл. Баннер накладывается на видео, а видеовставка проигрывается в середине клипа.
      </p>
      <label className="block text-xs max-w-md">Название рекламы (необязательно)
        <input className={fieldClass} data-testid="ad-name" value={adName} maxLength={100} disabled={Boolean(pending)}
          onChange={(event) => setAdName(event.target.value)} />
      </label>
      <div className="flex flex-wrap gap-2 items-center">
        <UploadAsset kind="banner" onSelect={uploadAsset} busy={pending} />
        <UploadAsset kind="video" onSelect={uploadAsset} busy={pending} />
        <span className="text-xs text-[var(--cf-text-2)]">Баннер PNG, JPG или WebP до 10 МБ</span>
      </div>
      <label className="flex items-center gap-2 text-xs">
        <input data-testid="ad-enabled-checkbox" type="checkbox" checked={Boolean(settings.ad_enabled)}
          disabled={Boolean(pending) || (!selectedAd && !settings.ad_enabled)}
          onChange={(event) => onChange({ ad_enabled: event.target.checked })} />
        Добавлять выбранную рекламу в новые ролики
      </label>
      {selectedAd && <div className="space-y-2">
        <label className="block text-xs max-w-md">Тип рекламы для отметки на площадке
          <select className={fieldClass} data-testid="ad-disclosure" value={settings.ad_disclosure || ""}
            onChange={(event) => onChange({ ad_disclosure: event.target.value })}>
            <option value="">Выберите тип рекламы</option>
            <option value="own">Свой бренд</option>
            <option value="sponsored">Платное партнёрство</option>
          </select>
        </label>
        <p className="text-xs text-[var(--cf-text-2)]">
          Автопост ищет отметку для обоих типов в TikTok и для платного партнёрства в YouTube. Если подходящая отметка недоступна, опубликуйте выпуск вручную с раскрытием рекламы.
        </p>
      </div>}
      {ads.length > 0 && <div className="space-y-2">
        {ads.map((asset) => {
          const isBanner = asset.kind === "banner";
          const url = `${API}/ads/${asset.id}/file`;
          const chosen = settings.ad_id === asset.id;
          return <div key={asset.id} className={`flex flex-wrap items-center gap-3 rounded-md border px-3 py-2 text-xs ${chosen ? "border-[var(--cf-cyan)]" : "border-[var(--cf-border)]"}`}>
            <label className="flex min-w-0 flex-1 items-center gap-2">
              <input data-testid={`ad-select-${asset.id}`} type="radio" name="ad" checked={chosen} disabled={Boolean(pending)}
                onChange={() => onChange({ ad_id: asset.id })} />
              {isBanner && <img src={url} alt="" loading="lazy" data-testid={`banner-preview-${asset.id}`}
                className="w-20 h-12 object-contain bg-[var(--cf-bg)] rounded" />}
              <span className="min-w-0">
                <span className="block break-words">{asset.name}</span>
                <span className="block text-[var(--cf-text-2)] mt-1">
                  {isBanner ? "Рекламный баннер" : "Видеовставка"}
                  {asset.size_mb != null && ` · ${asset.size_mb} МБ`}
                </span>
              </span>
            </label>
            <a href={url} target="_blank" rel="noreferrer" className="text-[var(--cf-cyan)] hover:underline">Открыть файл</a>
            <button type="button" data-testid={`delete-ad-${asset.id}`} disabled={Boolean(pending)}
              onClick={() => deleteAsset(asset)} className="text-[var(--cf-text-2)] hover:text-red-400 disabled:opacity-50"
              aria-label={`Удалить ${asset.name}`}><Trash2 className="w-3.5 h-3.5" /></button>
          </div>;
        })}
      </div>}
    </section>

    <section className="border border-[var(--cf-border)] rounded-md p-4 space-y-3" aria-label="Стиль ролика">
      <h4 className="text-sm font-semibold">Стиль ролика</h4>
      <label className="block text-xs max-w-md">Компоновка
        <select className={fieldClass} data-testid="retention-style" value={split ? "split" : "clean"}
          onChange={(event) => onChange({ retention_style: event.target.value })}>
          <option value="clean">Один кадр</option>
          <option value="split">Два кадра для разговорных клипов</option>
        </select>
      </label>
      {split && <div className="space-y-3">
        <p className="text-xs text-[var(--cf-text-2)]">
          В разговорных клипах нижняя панель воспроизводит выбранный MP4 без звука.
        </p>
        <label className="block text-xs max-w-md">Нижний ролик
          <select className={fieldClass} data-testid="retention-loop" value={selectedLoop?.id || ""} disabled={Boolean(pending)}
            onChange={(event) => onChange({ retention_loop_id: event.target.value })}>
            <option value="">Выберите MP4</option>
            {loops.map((asset) => <option key={asset.id} value={asset.id}>{asset.name}</option>)}
          </select>
        </label>
        <div className="flex flex-wrap gap-3 items-center">
          <UploadAsset kind="loop" onSelect={uploadAsset} busy={pending} />
          {selectedLoop && <>
            <a className="text-xs text-[var(--cf-cyan)] hover:underline" href={`${API}/ads/${selectedLoop.id}/file`} target="_blank" rel="noreferrer">Открыть MP4</a>
            <button type="button" className={buttonClass} disabled={Boolean(pending)} onClick={() => deleteAsset(selectedLoop)}>Удалить файл</button>
          </>}
        </div>
      </div>}
      <label className="flex items-center gap-2 text-xs">
        <input type="checkbox" data-testid="hook-sound" checked={Boolean(settings.hook_sound)}
          onChange={(event) => onChange({ hook_sound: event.target.checked })} />
        Звуковой акцент в начале
      </label>
    </section>
  </div>;
}
