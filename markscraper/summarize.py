"""Generate short summaries for segments that carry prose.

This is a separate phase from scraping: the archive lands in the database first
and can be summarised later, in whole or in part, with whichever model you pick.
The backfill uses the Batch API (half price); --sync is for small incremental
runs such as a newly published week.
"""
from __future__ import annotations

import time

from . import config

SYSTEM_PROMPT = (
    "You summarise segment write-ups from a Howard Stern Show fan archive. "
    "Given a segment title and its write-up, reply with one or two plain "
    "sentences capturing what actually happened: who was involved and what "
    "they did or said. Use past tense. Do not add a preamble, a heading, or "
    "commentary of your own. If the write-up is too short or empty to "
    "summarise, reply with the title alone."
)

# Models that reject output_config.effort.
NO_EFFORT = {"claude-haiku-4-5"}


def _client():
    import anthropic
    return anthropic.Anthropic()


def _todo(conn, args):
    sql = ("SELECT s.id, s.title, s.body_text, sh.show_date "
           "FROM segments s JOIN shows sh ON sh.id = s.show_id "
           "WHERE s.body_text IS NOT NULL AND s.summary IS NULL "
           "  AND s.batch_id IS NULL")
    params: list = []
    if args.since:
        sql += " AND sh.show_date >= ?"
        params.append(f"{args.since}-01-01")
    if args.until:
        sql += " AND sh.show_date <= ?"
        params.append(f"{args.until}-12-31")
    sql += " ORDER BY sh.show_date, s.id"
    if args.limit:
        sql += f" LIMIT {int(args.limit)}"
    return conn.execute(sql, params).fetchall()


def _user_content(row) -> str:
    return f"Title: {row['title']}\n\nWrite-up:\n{row['body_text']}"


def _params(args, row):
    """Build MessageCreateParams for one segment."""
    body = {
        "model": args.model,
        "max_tokens": 300,
        "system": [{"type": "text", "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": _user_content(row)}],
    }
    if args.model not in NO_EFFORT:
        body["output_config"] = {"effort": args.effort}
    return body


def _text(message) -> str:
    return "\n".join(b.text for b in message.content if b.type == "text").strip()


def _save(conn, seg_id, summary, model, effort):
    conn.execute(
        "UPDATE segments SET summary=?, summary_model=?, summary_effort=?, "
        "summarized_at=datetime('now'), batch_id=NULL WHERE id=?",
        (summary, model, effort, seg_id),
    )


def dry_run(conn, args) -> None:
    rows = _todo(conn, args)
    if not rows:
        print("nothing to summarise")
        return
    client = _client()
    sample = rows[:: max(1, len(rows) // 25)][:25]
    total = 0
    for row in sample:
        total += client.messages.count_tokens(
            model=args.model,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": _user_content(row)}],
        ).input_tokens
    avg_in = total / len(sample)
    in_tok = avg_in * len(rows)
    out_tok = 80 * len(rows)
    price_in, price_out = config.MODEL_PRICING.get(args.model, (0, 0))
    full = in_tok / 1e6 * price_in + out_tok / 1e6 * price_out
    print(f"segments to summarise : {len(rows):,}")
    print(f"sampled               : {len(sample)} segments, avg {avg_in:,.0f} input tokens")
    print(f"projected input       : {in_tok/1e6:,.1f}M tokens")
    print(f"projected output      : {out_tok/1e6:,.1f}M tokens (80/segment)")
    if price_in:
        print(f"model                 : {args.model}")
        print(f"estimated cost        : ${full:,.2f} live / ${full/2:,.2f} via batch")
    else:
        print(f"model                 : {args.model} (no local pricing table entry)")


def run_sync(conn, args, rows) -> None:
    import anthropic
    client = _client()
    done = 0
    for row in rows:
        try:
            message = client.messages.create(**_params(args, row))
        except anthropic.RateLimitError as exc:
            wait = int(exc.response.headers.get("retry-after", "30"))
            print(f"  rate limited, sleeping {wait}s")
            time.sleep(wait)
            message = client.messages.create(**_params(args, row))
        except anthropic.APIStatusError as exc:
            print(f"  segment {row['id']}: API error {exc.status_code}")
            continue
        except anthropic.APIConnectionError as exc:
            print(f"  segment {row['id']}: connection error {exc}")
            continue
        if message.stop_reason == "refusal":
            print(f"  segment {row['id']}: refused")
            continue
        _save(conn, row["id"], _text(message), args.model, args.effort)
        done += 1
        if done % 20 == 0:
            conn.commit()
            print(f"  {done}/{len(rows)}", flush=True)
    conn.commit()
    print(f"summarised {done} segments")


def run_batch(conn, args, rows) -> None:
    from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
    from anthropic.types.messages.batch_create_params import Request

    client = _client()
    chunks = [rows[i:i + args.chunk] for i in range(0, len(rows), args.chunk)]
    print(f"submitting {len(rows):,} segments in {len(chunks)} batch(es)")

    for n, chunk in enumerate(chunks, 1):
        batch = client.messages.batches.create(requests=[
            Request(custom_id=f"seg-{row['id']}",
                    params=MessageCreateParamsNonStreaming(**_params(args, row)))
            for row in chunk
        ])
        # Claim the segments before anything else can, so --resume can find them.
        with conn:
            conn.execute(
                "INSERT INTO summary_batches(batch_id, model, effort, request_count, "
                "status, created_at) VALUES (?,?,?,?,?,datetime('now'))",
                (batch.id, args.model, args.effort, len(chunk), batch.processing_status),
            )
            conn.executemany("UPDATE segments SET batch_id=? WHERE id=?",
                             [(batch.id, row["id"]) for row in chunk])
        print(f"  batch {n}/{len(chunks)}: {batch.id} ({len(chunk)} requests)")

    if args.wait:
        poll(conn, args, wait=True)
    else:
        print("\nrun `markscraper summarize --resume` to collect results "
              "(most batches finish within an hour)")


def poll(conn, args, wait: bool = False) -> None:
    client = _client()
    pending = conn.execute(
        "SELECT batch_id, model, effort FROM summary_batches "
        "WHERE retrieved_at IS NULL"
    ).fetchall()
    if not pending:
        print("no batches awaiting collection")
        return

    for row in pending:
        batch_id = row["batch_id"]
        while True:
            batch = client.messages.batches.retrieve(batch_id)
            conn.execute("UPDATE summary_batches SET status=? WHERE batch_id=?",
                         (batch.processing_status, batch_id))
            conn.commit()
            if batch.processing_status == "ended":
                break
            if not wait:
                print(f"{batch_id}: {batch.processing_status} "
                      f"(processing {batch.request_counts.processing})")
                break
            print(f"{batch_id}: {batch.processing_status}, "
                  f"processing {batch.request_counts.processing}", flush=True)
            time.sleep(60)
        if batch.processing_status != "ended":
            continue

        saved = errored = 0
        # Results come back in arbitrary order, so key on custom_id.
        for result in client.messages.batches.results(batch_id):
            seg_id = int(result.custom_id.removeprefix("seg-"))
            if result.result.type == "succeeded":
                message = result.result.message
                if message.stop_reason == "refusal":
                    errored += 1
                    continue
                _save(conn, seg_id, _text(message), row["model"], row["effort"])
                saved += 1
            else:
                errored += 1
        conn.execute(
            "UPDATE segments SET batch_id=NULL WHERE batch_id=? AND summary IS NULL",
            (batch_id,))
        conn.execute(
            "UPDATE summary_batches SET retrieved_at=datetime('now') WHERE batch_id=?",
            (batch_id,))
        conn.commit()
        print(f"{batch_id}: saved {saved}, failed {errored}")


def run(args, conn) -> None:
    try:
        _run(args, conn)
    except TypeError as exc:
        # Credentials resolve on the first request, not at construction.
        if "authentication" not in str(exc).lower():
            raise
        raise SystemExit(
            "No API credentials found. Set ANTHROPIC_API_KEY before summarising."
        ) from exc


def _run(args, conn) -> None:
    if args.resume:
        poll(conn, args, wait=args.wait)
        return
    if args.dry_run:
        dry_run(conn, args)
        return
    rows = _todo(conn, args)
    if not rows:
        print("nothing to summarise")
        return
    if args.sync:
        run_sync(conn, args, rows)
    else:
        run_batch(conn, args, rows)
