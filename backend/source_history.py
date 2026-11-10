"""Keep completed checks and confirmed publications separate from scheduling."""

from jobs import TERMINAL
from trend_sources import source_id


def _job_source(job):
    return source_id(job.get("video_url")) or source_id((job.get("source_video") or {}).get("video_id"))


async def excluded_source_ids(db, profile_id=None):
    query = {"profile_id": profile_id} if profile_id is not None else {}
    jobs = await db.jobs.find(query).to_list(None)
    jobs_by_id = {job["id"]: job for job in jobs}
    excluded = {source_id(row.get("video_id")) for row in await db.processed.find(
        {**query, "completed": True}).to_list(None)}
    excluded.update(_job_source(job) for job in jobs if job.get("status") not in TERMINAL)

    for post in await db.browser_posts.find({"status": "published"}).to_list(None):
        owner = post.get("profile_id") or post.get("scope")
        if profile_id is not None and owner and owner != profile_id:
            continue
        identity = source_id(post.get("source_id"))
        if not identity or not owner:
            clip_id = post.get("clip_id") or ""
            clip = await db.clips.find_one({"id": clip_id}) or {}
            job_id = clip.get("job_id") or clip_id.rsplit("_", 1)[0]
            job = jobs_by_id.get(job_id, {})
            owner = owner or clip.get("profile_id") or job.get("profile_id")
            identity = identity or source_id(clip.get("video_url")) or _job_source(job)
        if profile_id is None or owner == profile_id:
            excluded.add(identity)

    excluded.discard("")
    return excluded
