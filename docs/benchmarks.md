# Benchmarks

We ran upstream (`873309b`, lite build) and this fork side by side. Each run
started a fresh container with the same synthetic feeds (3 feeds × 500 episodes,
served locally) and hit each route with 8 concurrent clients for 30 s. We ran 3
rounds, rotating the order each round, and report the medians.

| | Upstream | This fork | Change |
|---|---|---|---|
| Memory, idle | 406 MiB | 86 MiB | 4.7× less |
| Memory, after 1,500 episodes ingested | 439 MiB | 96 MiB | 4.6× less |
| Memory, 60 s after load | 468 MiB | 98 MiB | 4.8× less |
| Memory, peak during feed-XML load | 526 MiB | 342 MiB | transient |
| Feed XML (`/feed/<id>`, 500 items) | 0.3 req/s · p50 38.3 s | 125 req/s · p50 56 ms | ≈400× throughput |
| Feed XML with `Accept-Encoding: gzip` | 0.3 req/s (no gzip) | 235 req/s · p50 33 ms | |
| Episode list (`/api/feeds/<id>/posts`) | 235 req/s · p50 32 ms | 573 req/s · p50 13 ms | 2.4× throughput |
| Add a 500-episode feed | 1.25 s | 0.17 s | 7× faster |
| Ready after start | 6.5 s | 4.5 s | |
| Image size | 1.74 GB (lite) · 13.7 GB (default) | 1.93 GB | |

Notes:

- Both images ran with `REQUIRE_AUTH=false` and `SERVER_THREADS=8`.
- Upstream's feed XML runs `ffprobe` on every processed episode's audio on every
  request, so its cost grows with the number of processed episodes. The Rust
  renderer doesn't probe.
- Memory is `docker stats` usage, which excludes page cache.
- The synthetic episode notes repeat, so the gzip row flatters: they shrink from
  811 KB to 12 KB. A real 3.7 MB feed compresses about 10×.

The full results, including fork `main` as a middle column, are in
[PR #62](https://github.com/MaroonBrian1928/podly_pure_podcasts_x/pull/62).
