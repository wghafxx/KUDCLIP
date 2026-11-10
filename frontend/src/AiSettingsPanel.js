import { useCallback, useEffect, useState } from "react";
import { Cpu, Loader2 } from "lucide-react";
import { toast } from "sonner";
import { api } from "@/lib/api";

const field = "w-full mt-1 bg-[var(--cf-bg)] border border-[var(--cf-border)] rounded-md px-3 py-2 text-sm outline-none focus:border-[var(--cf-cyan)]";
const ok = "text-emerald-400";
const warn = "text-amber-400";

export default function AiSettingsPanel() {
  const [state, setState] = useState(null);
  const [busy, setBusy] = useState(false);
  const load = useCallback(async () => {
    try { setState((await api.get("/ai")).data); }
    catch { toast.error("Не удалось загрузить настройки ИИ"); }
  }, []);
  useEffect(() => { load(); }, [load]);
  const save = async (patch) => {
    setBusy(true);
    try { setState((await api.put("/ai", patch)).data); toast.success("Сохранено"); }
    catch { toast.error("Не удалось сохранить"); }
    finally { setBusy(false); }
  };
  if (!state) return <Loader2 className="w-4 h-4 animate-spin" />;
  const keys = state.groq_keys;
  const gpuSpeech = state.asr !== "groq" && state.local_gpu;
  const cpuSpeech = state.asr === "local" && !state.local_gpu;
  const speech = gpuSpeech ? "видеокарта этого ПК" : cpuSpeech ? "процессор этого ПК (резерв)" : "Groq";
  return <div className="space-y-4 text-xs" data-testid="ai-settings-panel">
    <p className="text-[var(--cf-text-2)]">Всё бесплатно. Платных сервисов в программе нет.</p>
    <ul className="space-y-1" data-testid="ai-status">
      <li>Речь: <b className={cpuSpeech ? warn : ok}>{speech}</b></li>
      <li>Оценка моментов: <b className={keys >= 1 ? ok : warn}>Groq, подтверждённых ключей: {keys}</b></li>
      <li>Запасной ИИ на ПК: <b className={state.ollama_ready ? ok : warn}>{state.ollama_ready ? `Ollama работает (${state.ollama_model})` : "Ollama не запущена"}</b></li>
    </ul>
    <p className="text-[var(--cf-text-2)]">Для старта достаточно одного ключа бесплатного аккаунта Groq. Дополнительные квоты пригодятся, если очередь регулярно ждёт их обновления.</p>
    <div className="grid md:grid-cols-2 gap-4">
      <label>Кто оценивает моменты
        <select data-testid="ai-llm-select" className={field} value={state.llm} disabled={busy} onChange={(e) => save({ llm: e.target.value })}>
          <option value="auto">Groq, а когда лимит кончился — Ollama на ПК</option>
          <option value="groq">Только Groq</option>
          <option value="ollama">Только Ollama на ПК</option>
        </select>
      </label>
      <label>Распознавание речи
        <select data-testid="ai-asr-select" className={field} value={state.asr} disabled={busy} onChange={(e) => save({ asr: e.target.value })}>
          <option value="auto">Видеокарта, если есть, иначе Groq</option>
          <option value="local">Всегда на этом ПК</option>
          <option value="groq">Groq для коротких источников</option>
        </select>
      </label>
      <label>Модель распознавания на ПК
        <select data-testid="ai-whisper-select" className={field} value={state.whisper_model} disabled={busy} onChange={(e) => save({ whisper_model: e.target.value })}>
          <option value="large-v3-turbo">large-v3-turbo — быстро и точно (для 8 ГБ)</option>
          <option value="large-v3">large-v3 — точнее, медленнее</option>
          <option value="medium">medium — быстрее</option>
          <option value="small">small — экономный режим для длинных записей</option>
        </select>
      </label>
      <label>Модель Ollama
        <input data-testid="ai-ollama-model-input" className={field} defaultValue={state.ollama_model}
          onBlur={(e) => e.target.value.trim() && e.target.value.trim() !== state.ollama_model && save({ ollama_model: e.target.value.trim() })} />
      </label>
    </div>
    <p className="flex gap-2 text-[10px] text-[var(--cf-text-2)]"><Cpu className="w-3.5 h-3.5 shrink-0" />
      Длинные записи распознаются локально. При 70 °C KudClip останавливает обработку и выключает сервер. При следующем запуске работа продолжится с сохранённого этапа.</p>
  </div>;
}
