import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import os
import shutil


class DataStoreError(RuntimeError):
    pass


def _match(doc, query):
    for k, v in (query or {}).items():
        if isinstance(v, dict):
            if "$ne" in v and doc.get(k) == v["$ne"]:
                return False
        elif doc.get(k) != v:
            return False
    return True


def _apply(doc, update):
    for k, v in (update.get("$set") or {}).items():
        doc[k] = v
    for k, v in (update.get("$inc") or {}).items():
        doc[k] = doc.get(k, 0) + v
    for k, v in (update.get("$addToSet") or {}).items():
        vals = v["$each"] if isinstance(v, dict) and "$each" in v else [v]
        cur = doc.setdefault(k, [])
        for x in vals:
            if x not in cur:
                cur.append(x)


class _Cursor:
    def __init__(self, docs):
        self._docs = docs

    def sort(self, key, direction=1):
        self._docs = sorted(self._docs, key=lambda d: (d.get(key) is None, d.get(key)), reverse=direction == -1)
        return self

    async def to_list(self, length=None):
        return self._docs if length is None else self._docs[: length]


class Collection:
    def __init__(self, path: Path, lock: asyncio.Lock):
        self._path = path
        self._lock = lock

    def _load(self):
        if self._path.exists():
            try:
                result = json.loads(self._path.read_text(encoding="utf-8"))
                if not isinstance(result, list) or any(not isinstance(item, dict) for item in result):
                    raise ValueError('Invalid collection structure')
                return result
            except (ValueError, OSError) as exc:
                raise DataStoreError(f"Не удалось прочитать {self._path.name}. Данные сохранены; проверьте резервную копию .bak") from exc
        return []

    def _save(self, docs):
        # Validate the existing file before replacing it; a broken file must never
        # silently become an empty collection on the next write.
        if self._path.exists():
            self._load()
            shutil.copy2(self._path, self._path.with_suffix('.json.bak'))
        tmp = self._path.with_suffix(".tmp")
        with tmp.open('w', encoding='utf-8') as stream:
            json.dump(docs, stream, ensure_ascii=False, indent=1, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        tmp.replace(self._path)

    async def insert_one(self, doc):
        clean = json.loads(json.dumps({k: v for k, v in doc.items() if k != "_id"}, ensure_ascii=False, default=str))
        async with self._lock:
            docs = self._load()
            docs.append(clean)
            self._save(docs)
        return SimpleNamespace(inserted_id=clean.get("id"))

    async def find_one(self, query=None, projection=None):
        async with self._lock:
            for d in self._load():
                if _match(d, query):
                    return json.loads(json.dumps(d, ensure_ascii=False))
        return None

    def find(self, query=None, projection=None):
        docs = [json.loads(json.dumps(d, ensure_ascii=False)) for d in self._load() if _match(d, query)]
        return _Cursor(docs)

    async def update_one(self, query, update, upsert=False):
        async with self._lock:
            docs = self._load()
            for d in docs:
                if _match(d, query):
                    _apply(d, update)
                    self._save(docs)
                    return SimpleNamespace(matched_count=1, modified_count=1)
            if upsert:
                new_doc = {k: v for k, v in query.items() if not isinstance(v, dict)}
                _apply(new_doc, update)
                docs.append(new_doc)
                self._save(docs)
                return SimpleNamespace(matched_count=0, modified_count=0, upserted_id=True)
        return SimpleNamespace(matched_count=0, modified_count=0)

    async def delete_one(self, query):
        async with self._lock:
            docs = self._load()
            for i, d in enumerate(docs):
                if _match(d, query):
                    docs.pop(i)
                    self._save(docs)
                    return SimpleNamespace(deleted_count=1)
        return SimpleNamespace(deleted_count=0)

    async def delete_many(self, query):
        async with self._lock:
            docs = self._load()
            kept = [d for d in docs if not _match(d, query)]
            self._save(kept)
        return SimpleNamespace(deleted_count=len(docs) - len(kept))


class Database:
    def __init__(self, data_dir):
        self._dir = Path(data_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self._collections = {}

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        if name not in self._collections:
            self._collections[name] = Collection(self._dir / f"{name}.json", self._lock)
        return self._collections[name]
