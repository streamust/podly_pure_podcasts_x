use anyhow::{anyhow, Context, Result};
use chrono::{DateTime, NaiveDateTime, Utc};
use clap::{ArgAction, Args, Parser, Subcommand, ValueEnum};
use id3::frame::{Chapter as Id3Chapter, Content, Frame, TableOfContents};
use id3::{Encoding as Id3Encoding, Tag, TagLike, Version};
use regex::Regex;
use rusqlite::{types::ValueRef, Connection, OpenFlags, OptionalExtension};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::{HashMap, HashSet};
use std::fs;
use std::io::{BufWriter, Write};
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::{Mutex, OnceLock};

#[derive(Parser)]
#[command(name = "podly_tools")]
#[command(about = "Short-lived Podly helper process for file-heavy work")]
struct Cli {
    #[command(subcommand)]
    command: Commands,
}

#[derive(Subcommand)]
enum Commands {
    Audio(AudioCommand),
    Costs(CostsCommand),
    Feed(FeedCommand),
    Jobs(JobsCommand),
    Posts(PostsCommand),
    Stats(StatsCommand),
    Transcript(TranscriptCommand),
    Chapters(ChaptersCommand),
}

#[derive(Args)]
struct CostsCommand {
    #[command(subcommand)]
    command: CostsSubcommand,
}

#[derive(Subcommand)]
enum CostsSubcommand {
    RenderAdmin(CostsRenderAdminArgs),
    RenderCalls(CostsRenderCallsArgs),
}

#[derive(Args)]
struct CostsRenderAdminArgs {
    #[arg(long)]
    db: PathBuf,
    #[arg(long)]
    year: i32,
    #[arg(long)]
    month: u32,
    #[arg(long = "rates-json")]
    rates_json: PathBuf,
}

#[derive(Args)]
struct CostsRenderCallsArgs {
    #[arg(long)]
    db: PathBuf,
    #[arg(long, default_value_t = 1)]
    page: i64,
    #[arg(long = "per-page", default_value_t = 50)]
    per_page: i64,
    #[arg(long = "rates-json")]
    rates_json: PathBuf,
}

#[derive(Args)]
struct AudioCommand {
    #[command(subcommand)]
    command: AudioSubcommand,
}

#[derive(Subcommand)]
enum AudioSubcommand {
    Probe(AudioProbeArgs),
    Cut(AudioCutArgs),
    Bleep(AudioBleepArgs),
    Split(AudioSplitArgs),
}

#[derive(Args)]
struct AudioProbeArgs {
    #[arg(long)]
    input: PathBuf,
}

#[derive(Args)]
struct AudioCutArgs {
    #[arg(long)]
    input: PathBuf,
    #[arg(long)]
    output: PathBuf,
    #[arg(long = "windows-json")]
    windows_json: PathBuf,
    #[arg(long)]
    mode: CutMode,
    #[arg(long = "fade-ms")]
    fade_ms: u32,
    #[arg(long)]
    encoding: Encoding,
    #[arg(long = "cbr-bitrate-bps")]
    cbr_bitrate_bps: Option<u64>,
    #[arg(long = "vbr-quality")]
    vbr_quality: Option<u8>,
}

#[derive(Args)]
struct AudioBleepArgs {
    #[arg(long)]
    input: PathBuf,
    #[arg(long)]
    output: PathBuf,
    #[arg(long = "windows-json")]
    windows_json: PathBuf,
    #[arg(long = "beep-frequency-hz")]
    beep_frequency_hz: u32,
    #[arg(long = "beep-volume")]
    beep_volume: f32,
    #[arg(long = "duck-volume")]
    duck_volume: f32,
    #[arg(long = "fade-ms", default_value_t = 5)]
    fade_ms: u32,
    #[arg(long)]
    encoding: Encoding,
    #[arg(long = "cbr-bitrate-bps")]
    cbr_bitrate_bps: Option<u64>,
    #[arg(long = "vbr-quality")]
    vbr_quality: Option<u8>,
}

#[derive(Args)]
struct AudioSplitArgs {
    #[arg(long)]
    input: PathBuf,
    #[arg(long = "out-dir")]
    out_dir: PathBuf,
    #[arg(long = "chunk-size-bytes")]
    chunk_size_bytes: u64,
}

#[derive(Clone, ValueEnum)]
enum CutMode {
    Fade,
    Exact,
}

#[derive(Clone, ValueEnum)]
enum Encoding {
    Vbr,
    Cbr,
}

#[derive(Clone)]
struct EncodingOptions {
    mode: Encoding,
    cbr_bitrate_bps: Option<u64>,
    vbr_quality: Option<u8>,
}

impl EncodingOptions {
    fn new(mode: Encoding, cbr_bitrate_bps: Option<u64>, vbr_quality: Option<u8>) -> Self {
        Self {
            mode,
            cbr_bitrate_bps,
            vbr_quality,
        }
    }
}

#[derive(Args)]
struct FeedCommand {
    /// Emit RSS directly instead of wrapping it in a JSON string.
    #[arg(long, global = true)]
    raw_xml: bool,
    #[command(subcommand)]
    command: FeedSubcommand,
}

#[derive(Subcommand)]
enum FeedSubcommand {
    Render(FeedRenderArgs),
    RenderAggregate(FeedRenderAggregateArgs),
    RefreshPlan(FeedRefreshPlanArgs),
}

#[derive(Args)]
struct JobsCommand {
    #[command(subcommand)]
    command: JobsSubcommand,
}

#[derive(Subcommand)]
enum JobsSubcommand {
    Active(JobsListArgs),
    All(JobsListArgs),
    Status(JobsStatusArgs),
}

#[derive(Args)]
struct JobsListArgs {
    #[arg(long)]
    db: PathBuf,
    #[arg(long, default_value_t = 100)]
    limit: i64,
}

#[derive(Args)]
struct JobsStatusArgs {
    #[arg(long)]
    db: PathBuf,
}

#[derive(Args)]
struct StatsCommand {
    #[command(subcommand)]
    command: StatsSubcommand,
}

#[derive(Subcommand)]
enum StatsSubcommand {
    Render(StatsRenderArgs),
}

#[derive(Args)]
struct StatsRenderArgs {
    #[arg(long)]
    db: PathBuf,
    #[arg(long = "post-guid")]
    post_guid: String,
    #[arg(long = "min-confidence")]
    min_confidence: f64,
    #[arg(long = "min-ad-segment-separation-seconds")]
    min_ad_segment_separation_seconds: f64,
    #[arg(long = "enable-boundary-refinement", action = ArgAction::Set)]
    enable_boundary_refinement: bool,
    #[arg(long = "stats-debug", action = ArgAction::Set)]
    stats_debug: bool,
    #[arg(long = "log-path")]
    log_path: PathBuf,
    #[arg(long = "in-root")]
    in_root: PathBuf,
    #[arg(long = "srv-root")]
    srv_root: PathBuf,
}

#[derive(Args)]
struct FeedRenderArgs {
    #[arg(long)]
    db: PathBuf,
    #[arg(long = "feed-id")]
    feed_id: i64,
    #[arg(long = "base-url")]
    base_url: String,
    #[arg(long = "include-unprocessed")]
    include_unprocessed: bool,
    #[arg(long = "feed-token")]
    feed_token: Option<String>,
    #[arg(long = "feed-secret")]
    feed_secret: Option<String>,
}

#[derive(Args)]
struct FeedRenderAggregateArgs {
    #[arg(long)]
    db: PathBuf,
    #[arg(long = "user-id")]
    user_id: i64,
    #[arg(long = "base-url")]
    base_url: String,
    #[arg(long = "require-auth")]
    require_auth: bool,
    #[arg(long = "limit-per-feed")]
    limit_per_feed: usize,
    #[arg(long = "feed-token")]
    feed_token: Option<String>,
    #[arg(long = "feed-secret")]
    feed_secret: Option<String>,
}

#[derive(Args)]
struct PostsCommand {
    #[command(subcommand)]
    command: PostsSubcommand,
}

#[derive(Subcommand)]
enum PostsSubcommand {
    FeedList(PostsFeedListArgs),
}

#[derive(Args)]
struct PostsFeedListArgs {
    #[arg(long)]
    db: PathBuf,
    #[arg(long = "feed-id")]
    feed_id: i64,
    #[arg(long, default_value_t = 1)]
    page: i64,
    #[arg(long = "page-size", default_value_t = 25)]
    page_size: i64,
    #[arg(long = "whitelisted-only", action = ArgAction::Set)]
    whitelisted_only: bool,
}

#[derive(Args)]
struct FeedRefreshPlanArgs {
    #[arg(long)]
    db: PathBuf,
    #[arg(long = "feed-id")]
    feed_id: i64,
    #[arg(long = "feed-xml")]
    feed_xml: PathBuf,
    #[arg(long = "auto-whitelist-new-posts", action = ArgAction::Set)]
    auto_whitelist_new_posts: bool,
}

#[derive(Args)]
struct TranscriptCommand {
    #[command(subcommand)]
    command: TranscriptSubcommand,
}

#[derive(Subcommand)]
enum TranscriptSubcommand {
    NormalizeWordTimestamps(TranscriptNormalizeArgs),
    ExportWordTimestamps(TranscriptExportArgs),
    AdMerge(TranscriptAdMergeArgs),
    ProfanityWindows(TranscriptProfanityWindowsArgs),
    WbContext(TranscriptWbContextArgs),
    WbResolve(TranscriptWbResolveArgs),
    WbJsonParse(TranscriptWbJsonParseArgs),
    WbRefineFromLlm(TranscriptWbRefineFromLlmArgs),
    RepeatAdCandidates(TranscriptRepeatAdCandidatesArgs),
}

#[derive(Args)]
struct TranscriptRepeatAdCandidatesArgs {
    #[arg(long)]
    db: PathBuf,
    #[arg(long = "post-guid")]
    post_guid: String,
    #[arg(long = "target-first-seq")]
    target_first_seq: i64,
    #[arg(long = "target-last-seq")]
    target_last_seq: i64,
    #[arg(long = "similarity-threshold")]
    similarity_threshold: f64,
    /// Path to a JSON file: {"exclude_ranges": [[first, last], ...]}.
    #[arg(long)]
    input: PathBuf,
}

#[derive(Args)]
struct TranscriptWbRefineFromLlmArgs {
    #[arg(long)]
    db: PathBuf,
    #[arg(long = "post-guid")]
    post_guid: String,
    #[arg(long = "orig-ad-start")]
    orig_ad_start: f64,
    #[arg(long = "orig-ad-end")]
    orig_ad_end: f64,
    #[arg(long = "first-seq")]
    first_seq: Option<i64>,
    #[arg(long = "last-seq")]
    last_seq: Option<i64>,
    /// Path to a UTF-8 text file containing the raw LLM completion.
    #[arg(long = "raw-content-file")]
    raw_content_file: PathBuf,
}

#[derive(Args)]
struct TranscriptWbJsonParseArgs {
    /// Path to a UTF-8 text file containing the raw LLM completion.
    #[arg(long)]
    input: PathBuf,
}

#[derive(Args)]
struct TranscriptWbContextArgs {
    #[arg(long)]
    db: PathBuf,
    #[arg(long = "post-guid")]
    post_guid: String,
    #[arg(long = "ad-start")]
    ad_start: f64,
    #[arg(long = "ad-end")]
    ad_end: f64,
    #[arg(long = "first-seq")]
    first_seq: Option<i64>,
    #[arg(long = "last-seq")]
    last_seq: Option<i64>,
}

#[derive(Args)]
struct TranscriptWbResolveArgs {
    #[arg(long)]
    db: PathBuf,
    #[arg(long = "post-guid")]
    post_guid: String,
    #[arg(long = "orig-ad-start")]
    orig_ad_start: f64,
    #[arg(long = "orig-ad-end")]
    orig_ad_end: f64,
    #[arg(long = "first-seq")]
    first_seq: Option<i64>,
    #[arg(long = "last-seq")]
    last_seq: Option<i64>,
    #[arg(long = "start-segment-seq")]
    start_segment_seq: Option<i64>,
    #[arg(long = "start-phrase")]
    start_phrase: Option<String>,
    #[arg(long = "start-word")]
    start_word: Option<String>,
    #[arg(long = "start-occurrence")]
    start_occurrence: Option<String>,
    #[arg(long = "start-word-index")]
    start_word_index: Option<i64>,
    #[arg(long = "end-segment-seq")]
    end_segment_seq: Option<i64>,
    #[arg(long = "end-phrase")]
    end_phrase: Option<String>,
}

#[derive(Args)]
struct TranscriptProfanityWindowsArgs {
    #[arg(long)]
    input: PathBuf,
}

#[derive(Args)]
struct TranscriptAdMergeArgs {
    #[arg(long)]
    db: PathBuf,
    #[arg(long = "post-guid")]
    post_guid: String,
    #[arg(long = "min-confidence")]
    min_confidence: f64,
    #[arg(long = "max-gap")]
    max_gap: f64,
    #[arg(long = "enable-boundary-refinement", action = ArgAction::Set)]
    enable_boundary_refinement: bool,
}

#[derive(Args)]
struct TranscriptNormalizeArgs {
    #[arg(long)]
    input: PathBuf,
    #[arg(long)]
    output: PathBuf,
}

#[derive(Args)]
struct TranscriptExportArgs {
    #[arg(long)]
    db: PathBuf,
    #[arg(long = "post-id")]
    post_id: i64,
}

#[derive(Args)]
struct ChaptersCommand {
    #[command(subcommand)]
    command: ChaptersSubcommand,
}

#[derive(Subcommand)]
enum ChaptersSubcommand {
    Read(ChaptersReadArgs),
    Detect(ChaptersDetectArgs),
    Write(ChaptersWriteArgs),
    TopicBlocks(ChaptersTopicBlocksArgs),
    TopicPlanParse(ChaptersTopicPlanParseArgs),
    TopicPlanApply(ChaptersTopicPlanApplyArgs),
}

#[derive(Args)]
struct ChaptersTopicPlanParseArgs {
    /// Path to a UTF-8 text file containing the raw LLM completion.
    #[arg(long)]
    input: PathBuf,
}

#[derive(Args)]
struct ChaptersTopicPlanApplyArgs {
    /// Path to a JSON file with `{plan, blocks, total_duration_ms, min_chapter_gap_ms}`.
    #[arg(long)]
    input: PathBuf,
}

#[derive(Args)]
struct ChaptersTopicBlocksArgs {
    #[arg(long)]
    db: PathBuf,
    #[arg(long = "post-guid")]
    post_guid: String,
    #[arg(long = "target-block-count", default_value_t = 60)]
    target_block_count: i64,
    #[arg(long = "min-block-seconds", default_value_t = 60)]
    min_block_seconds: i64,
    #[arg(long = "max-block-seconds", default_value_t = 120)]
    max_block_seconds: i64,
    // Keep in sync with TOPIC_CHAPTER_MAX_CHARS_PER_BLOCK in
    // src/podcast_processor/chapter_fallback.py. The wrapper passes this
    // explicitly so the CLI default is only used by direct CLI invocations.
    #[arg(long = "max-chars-per-block", default_value_t = 1000)]
    max_chars_per_block: i64,
    /// Prefix each segment's text with an inline `[sNNN]` marker so the LLM can
    /// name a precise chapter start anchor. Mirrors `include_segment_markers`
    /// in `_build_topic_blocks`.
    #[arg(
        long = "include-segment-markers",
        action = ArgAction::Set,
        default_value_t = false
    )]
    include_segment_markers: bool,
    /// Optional override; otherwise Rust computes from max(end_time) over segments.
    #[arg(long = "total-duration-ms")]
    total_duration_ms: Option<i64>,
    /// Optional JSON file: array of `{start_ms, end_ms}` windows whose
    /// overlapping transcript segments should be removed before block-building.
    /// Mirrors `_filter_transcript_segments_for_chapters` in Python. If the
    /// filter would empty the segment list, the unfiltered set is used
    /// instead (parity with Python).
    #[arg(long = "removed-windows-json")]
    removed_windows_json: Option<PathBuf>,
}

#[derive(Args)]
struct ChaptersReadArgs {
    #[arg(long)]
    audio: PathBuf,
}

#[derive(Args)]
struct ChaptersDetectArgs {
    #[arg(long)]
    audio: PathBuf,
    #[arg(long = "filter-strings-csv")]
    filter_strings_csv: String,
}

#[derive(Args)]
struct ChaptersWriteArgs {
    #[arg(long)]
    audio: PathBuf,
    #[arg(long = "chapters-json")]
    chapters_json: PathBuf,
    #[arg(long = "removed-windows-json")]
    removed_windows_json: PathBuf,
}

#[derive(Serialize)]
struct ProbeResponse {
    duration_ms: u64,
}

#[derive(Serialize)]
struct OkResponse {
    ok: bool,
}

#[derive(Serialize)]
struct SplitResponse {
    chunks: Vec<SplitChunk>,
}

#[derive(Serialize)]
struct SplitChunk {
    path: String,
    offset_ms: u64,
}

#[derive(Deserialize)]
struct WindowMs(u64, u64);

#[derive(Serialize)]
struct NormalizedWordSegment {
    sequence_num: i64,
    words: Vec<NormalizedWord>,
}

#[derive(Serialize)]
struct NormalizedWord {
    word: String,
    start: f64,
    end: f64,
    score: Option<f64>,
}

#[derive(Serialize)]
struct TranscriptExportResponse {
    transcript_word_timestamps: Value,
}

#[derive(Serialize)]
struct XmlResponse {
    xml: String,
}

struct FeedRow {
    id: i64,
    title: String,
    description: Option<String>,
    image_url: Option<String>,
    last_changed_at: Option<String>,
}

struct FeedRefreshFeedRow {
    id: i64,
    image_url: Option<String>,
}

struct FeedRefreshPostRow {
    id: i64,
    guid: String,
    download_url: String,
    title: String,
    description: Option<String>,
    processed_audio_path: Option<String>,
    release_date: Option<String>,
    duration: Option<i64>,
    image_url: Option<String>,
}

#[derive(Debug)]
struct ParsedFeedEntry {
    guid: String,
    title: String,
    description: String,
    download_url: String,
    release_date: Option<String>,
    duration: Option<i64>,
    image_url: Option<String>,
}

struct PostRow {
    feed_title: Option<String>,
    title: String,
    guid: String,
    processed_audio_path: Option<String>,
    description: Option<String>,
    release_date: Option<String>,
    duration: Option<i64>,
    image_url: Option<String>,
    chapter_data: Option<String>,
}

struct RssBuildArgs<'a> {
    title: &'a str,
    link: &'a str,
    description: &'a str,
    image_url: Option<&'a str>,
    image_title: &'a str,
    last_build_date: &'a str,
    base_url: &'a str,
    feed_token: Option<&'a str>,
    feed_secret: Option<&'a str>,
    posts: &'a [PostRow],
    prepend_feed_title: bool,
}

#[derive(Deserialize)]
struct ChapterPayload {
    title: String,
    start_time_ms: u32,
    end_time_ms: u32,
}

#[derive(Clone, Serialize)]
struct ChapterResponseItem {
    element_id: String,
    title: String,
    start_time_ms: u32,
    end_time_ms: u32,
}

#[derive(Serialize)]
struct ChaptersReadResponse {
    chapters: Vec<ChapterResponseItem>,
}

#[derive(Serialize)]
struct ChaptersDetectResponse {
    ad_segments: Vec<(f64, f64)>,
    chapters_to_keep: Vec<ChapterResponseItem>,
    chapters_to_remove: Vec<ChapterResponseItem>,
}

#[derive(Clone)]
struct StatsPostRow {
    id: i64,
    feed_id: i64,
    guid: String,
    title: String,
    download_url: String,
    unprocessed_audio_path: Option<String>,
    processed_audio_path: Option<String>,
    release_date: Option<String>,
    duration: Option<f64>,
    whitelisted: bool,
    download_count: Option<i64>,
    chapter_data: Option<String>,
    bleep_windows: Option<String>,
    refined_ad_boundaries: Option<String>,
}

struct StatsFeedRow {
    ad_detection_strategy: String,
    chapter_filter_strings: Option<String>,
}

#[derive(Clone)]
struct StatsTranscriptSegmentRow {
    id: i64,
    sequence_num: i64,
    start_time: f64,
    end_time: f64,
    text: String,
    speaker_label: Option<String>,
}

#[derive(Clone)]
struct StatsIdentificationRow {
    id: i64,
    transcript_segment_id: i64,
    label: String,
    confidence: Option<f64>,
    model_call_id: i64,
    model_call_status: String,
    segment_sequence_num: i64,
    segment_start_time: f64,
    segment_end_time: f64,
    segment_text: String,
}

#[derive(Clone)]
struct StatsAudioSegmentRow {
    id: i64,
    start_time: f64,
    end_time: f64,
    label: String,
    model_call_id: Option<i64>,
}

struct StatsModelCallRow {
    id: i64,
    model_name: String,
    status: String,
    first_segment_sequence_num: i64,
    last_segment_sequence_num: i64,
    timestamp: Option<String>,
    retry_attempts: i64,
    error_message: Option<String>,
    prompt: String,
    response: Option<String>,
    service_tier: Option<String>,
    prompt_tokens: Option<i64>,
    cached_prompt_tokens: Option<i64>,
    completion_tokens: Option<i64>,
    total_tokens: Option<i64>,
}

struct StatsProcessingJobRow {
    id: String,
}

#[derive(Clone)]
struct StatsAdGroup {
    segments: Vec<StatsTranscriptSegmentRow>,
    identifications: Vec<StatsIdentificationRow>,
    start_time: f64,
    end_time: f64,
    confidence_avg: f64,
    keywords: Vec<String>,
}

#[derive(Clone)]
struct RefinedBoundaryRow {
    orig_start: f64,
    orig_end: f64,
    refined_start: f64,
    refined_end: f64,
}

fn main() {
    if let Err(error) = run() {
        eprintln!("{error:#}");
        std::process::exit(1);
    }
}

fn run() -> Result<()> {
    let cli = Cli::parse();

    match cli.command {
        Commands::Audio(audio) => match audio.command {
            AudioSubcommand::Probe(args) => print_json(&probe_audio_path(&args.input)?),
            AudioSubcommand::Cut(args) => print_json(&cut_audio(args)?),
            AudioSubcommand::Bleep(args) => print_json(&bleep_audio(args)?),
            AudioSubcommand::Split(args) => print_json(&split_audio(args)?),
        },
        Commands::Costs(costs) => match costs.command {
            CostsSubcommand::RenderAdmin(args) => print_json(&render_admin_costs(args)?),
            CostsSubcommand::RenderCalls(args) => print_json(&render_admin_costs_calls(args)?),
        },
        Commands::Feed(feed) => match feed.command {
            FeedSubcommand::Render(args) => print_feed(&render_feed(args)?, feed.raw_xml),
            FeedSubcommand::RenderAggregate(args) => {
                print_feed(&render_aggregate_feed(args)?, feed.raw_xml)
            }
            FeedSubcommand::RefreshPlan(args) => print_json(&plan_feed_refresh(args)?),
        },
        Commands::Jobs(jobs) => match jobs.command {
            JobsSubcommand::Active(args) => print_json(&render_jobs(args, true)?),
            JobsSubcommand::All(args) => print_json(&render_jobs(args, false)?),
            JobsSubcommand::Status(args) => print_json(&render_jobs_status(args)?),
        },
        Commands::Posts(posts) => match posts.command {
            PostsSubcommand::FeedList(args) => print_json(&render_feed_posts(args)?),
        },
        Commands::Stats(stats) => match stats.command {
            StatsSubcommand::Render(args) => print_json(&render_stats(args)?),
        },
        Commands::Transcript(transcript) => match transcript.command {
            TranscriptSubcommand::NormalizeWordTimestamps(args) => {
                print_json(&normalize_word_timestamps(args)?)
            }
            TranscriptSubcommand::ExportWordTimestamps(args) => {
                print_json(&export_word_timestamps(args)?)
            }
            TranscriptSubcommand::AdMerge(args) => print_json(&run_transcript_ad_merge(args)?),
            TranscriptSubcommand::ProfanityWindows(args) => {
                print_json(&run_transcript_profanity_windows(args)?)
            }
            TranscriptSubcommand::WbContext(args) => print_json(&run_transcript_wb_context(args)?),
            TranscriptSubcommand::WbResolve(args) => print_json(&run_transcript_wb_resolve(args)?),
            TranscriptSubcommand::WbJsonParse(args) => {
                print_json(&run_transcript_wb_json_parse(args)?)
            }
            TranscriptSubcommand::WbRefineFromLlm(args) => {
                print_json(&run_transcript_wb_refine_from_llm(args)?)
            }
            TranscriptSubcommand::RepeatAdCandidates(args) => {
                print_json(&run_transcript_repeat_ad_candidates(args)?)
            }
        },
        Commands::Chapters(chapters) => match chapters.command {
            ChaptersSubcommand::Read(args) => print_json(&read_chapters(args)?),
            ChaptersSubcommand::Detect(args) => print_json(&detect_chapter_ads(args)?),
            ChaptersSubcommand::Write(args) => print_json(&write_chapters(args)?),
            ChaptersSubcommand::TopicBlocks(args) => print_json(&run_chapters_topic_blocks(args)?),
            ChaptersSubcommand::TopicPlanParse(args) => {
                print_json(&run_chapters_topic_plan_parse(args)?)
            }
            ChaptersSubcommand::TopicPlanApply(args) => {
                print_json(&run_chapters_topic_plan_apply(args)?)
            }
        },
    }
}

#[derive(Deserialize)]
struct ProfanityWord {
    word: Option<String>,
    start: Option<f64>,
    end: Option<f64>,
}

#[derive(Deserialize)]
struct ProfanityRequest {
    words: Vec<ProfanityWord>,
    profanity_terms: Vec<String>,
    pad_start_ms: i64,
    pad_end_ms: i64,
    merge_gap_ms: i64,
}

fn run_transcript_profanity_windows(args: TranscriptProfanityWindowsArgs) -> Result<Value> {
    let raw = std::fs::read_to_string(&args.input)
        .with_context(|| format!("failed to read profanity input {}", args.input.display()))?;
    let request: ProfanityRequest =
        serde_json::from_str(&raw).context("failed to parse profanity-windows input as JSON")?;

    static NORMALIZE_RE: OnceLock<Regex> = OnceLock::new();
    let normalize_re =
        NORMALIZE_RE.get_or_init(|| Regex::new(r"(^[^A-Za-z0-9']+)|([^A-Za-z0-9']+$)").unwrap());

    let terms: HashSet<String> = request
        .profanity_terms
        .into_iter()
        .map(|t| t.to_lowercase())
        .collect();

    let mut raw_windows: Vec<(i64, i64)> = Vec::new();
    for w in request.words {
        let (Some(text), Some(start), Some(end)) = (w.word, w.start, w.end) else {
            continue;
        };
        let normalized = normalize_re.replace_all(&text, "").to_lowercase();
        if normalized.is_empty() || !terms.contains(&normalized) {
            continue;
        }
        let start_ms = ((start * 1000.0).floor() as i64 - request.pad_start_ms).max(0);
        let end_ms = ((end * 1000.0).ceil() as i64 + request.pad_end_ms).max(start_ms + 1);
        raw_windows.push((start_ms, end_ms));
    }

    raw_windows.sort();
    let mut merged: Vec<(i64, i64)> = Vec::with_capacity(raw_windows.len());
    for (start, end) in raw_windows {
        if let Some(last) = merged.last_mut() {
            if start <= last.1 + request.merge_gap_ms {
                last.1 = last.1.max(end);
                continue;
            }
        }
        merged.push((start, end));
    }

    let windows: Vec<Value> = merged.into_iter().map(|(s, e)| json!([s, e])).collect();
    Ok(json!({ "windows_ms": windows }))
}

fn run_transcript_wb_context(args: TranscriptWbContextArgs) -> Result<Value> {
    let conn = open_readonly_sqlite(&args.db)?;
    let post = query_stats_post(&conn, &args.post_guid)?
        .ok_or_else(|| anyhow!("post not found for guid {}", args.post_guid))?;
    let segments = query_stats_transcript_segments(&conn, post.id)?;
    let selected = select_wb_context_segments(
        &segments,
        args.ad_start,
        args.ad_end,
        args.first_seq,
        args.last_seq,
    );
    let context_segments: Vec<Value> = selected
        .iter()
        .map(|seg| {
            json!({
                "sequence_num": seg.sequence_num,
                "start_time": seg.start_time,
                "end_time": seg.end_time,
                "text": seg.text,
            })
        })
        .collect();
    Ok(json!({ "context_segments": context_segments }))
}

// === Repeat-ad candidate finder: deterministic token-LCS matcher ported from
// `src/podcast_processor/repeat_ad_finder.py`. The LLM confirm call stays in
// Python; this finds spans whose text matches an already-detected ad. Parity
// matters — divergence would leave repeated ads uncut or cut the wrong span.

const REPEAT_AD_ANCHOR_SIMILARITY_THRESHOLD: f64 = 0.70;
const REPEAT_AD_MIN_TARGET_TOKENS: usize = 6;
const REPEAT_AD_MAX_WINDOW_SEGMENT_SLACK: usize = 2;

#[derive(Deserialize)]
struct RepeatAdCandidatesRequest {
    #[serde(default)]
    exclude_ranges: Vec<(i64, i64)>,
}

struct RepeatAdCandidateRow {
    first_seq: i64,
    last_seq: i64,
    start_time: f64,
    end_time: f64,
    similarity: f64,
}

fn repeat_ad_tokenize(text: &str) -> Vec<String> {
    // Same normalization as `repeat_ad_finder.tokenize` (lowercase + strip
    // edge punctuation, keep internal apostrophes).
    wb_split_words_lower(text)
}

fn repeat_ad_lcs_length(a: &[String], b: &[String]) -> usize {
    if a.is_empty() || b.is_empty() {
        return 0;
    }
    let mut prev = vec![0usize; b.len() + 1];
    for token_a in a {
        let mut cur = vec![0usize; b.len() + 1];
        for (j, token_b) in b.iter().enumerate() {
            cur[j + 1] = if token_a == token_b {
                prev[j] + 1
            } else {
                prev[j + 1].max(cur[j])
            };
        }
        prev = cur;
    }
    prev[b.len()]
}

fn repeat_ad_similarity(a: &[String], b: &[String]) -> f64 {
    if a.is_empty() && b.is_empty() {
        return 1.0;
    }
    if a.is_empty() || b.is_empty() {
        return 0.0;
    }
    let lcs = repeat_ad_lcs_length(a, b) as f64;
    (2.0 * lcs) / ((a.len() + b.len()) as f64)
}

fn find_repeat_ad_candidates(
    segments: &[StatsTranscriptSegmentRow],
    target_first_seq: i64,
    target_last_seq: i64,
    exclude_ranges: &[(i64, i64)],
    similarity_threshold: f64,
) -> Vec<RepeatAdCandidateRow> {
    if segments.is_empty() {
        return Vec::new();
    }

    let target_segs: Vec<&StatsTranscriptSegmentRow> = segments
        .iter()
        .filter(|s| s.sequence_num >= target_first_seq && s.sequence_num <= target_last_seq)
        .collect();
    if target_segs.is_empty() {
        return Vec::new();
    }

    let target_text = target_segs
        .iter()
        .map(|s| s.text.as_str())
        .collect::<Vec<_>>()
        .join(" ");
    let target_tokens = repeat_ad_tokenize(&target_text);
    if target_tokens.len() < REPEAT_AD_MIN_TARGET_TOKENS {
        return Vec::new();
    }
    let target_first_tokens = repeat_ad_tokenize(&target_segs[0].text);
    let max_window_segments = target_segs.len() + REPEAT_AD_MAX_WINDOW_SEGMENT_SLACK;

    let mut excluded: HashSet<i64> = (target_first_seq..=target_last_seq).collect();
    for (a, b) in exclude_ranges {
        for seq in *a..=*b {
            excluded.insert(seq);
        }
    }

    let mut candidates: Vec<RepeatAdCandidateRow> = Vec::new();
    let mut i = 0usize;
    while i < segments.len() {
        let seg = &segments[i];
        if excluded.contains(&seg.sequence_num) {
            i += 1;
            continue;
        }

        let anchor_tokens = repeat_ad_tokenize(&seg.text);
        if repeat_ad_similarity(&anchor_tokens, &target_first_tokens)
            < REPEAT_AD_ANCHOR_SIMILARITY_THRESHOLD
        {
            i += 1;
            continue;
        }

        let mut window: Vec<&StatsTranscriptSegmentRow> = Vec::new();
        let mut acc_tokens: Vec<String> = Vec::new();
        let mut j = i;
        while j < segments.len() && (j - i) < max_window_segments {
            let seg_j = &segments[j];
            if excluded.contains(&seg_j.sequence_num) {
                break;
            }
            window.push(seg_j);
            acc_tokens.extend(repeat_ad_tokenize(&seg_j.text));
            j += 1;
            if acc_tokens.len() >= target_tokens.len() {
                break;
            }
        }

        let window_similarity = repeat_ad_similarity(&acc_tokens, &target_tokens);
        if !window.is_empty() && window_similarity >= similarity_threshold {
            let first = window[0];
            let last = window[window.len() - 1];
            candidates.push(RepeatAdCandidateRow {
                first_seq: first.sequence_num,
                last_seq: last.sequence_num,
                start_time: first.start_time,
                end_time: last.end_time,
                similarity: window_similarity,
            });
            for matched in &window {
                excluded.insert(matched.sequence_num);
            }
            i = j;
        } else {
            i += 1;
        }
    }

    candidates
}

fn run_transcript_repeat_ad_candidates(args: TranscriptRepeatAdCandidatesArgs) -> Result<Value> {
    let raw = std::fs::read_to_string(&args.input).with_context(|| {
        format!(
            "failed to read repeat-ad-candidates input {}",
            args.input.display()
        )
    })?;
    let request: RepeatAdCandidatesRequest =
        serde_json::from_str(&raw).context("failed to parse repeat-ad-candidates input as JSON")?;

    let conn = open_readonly_sqlite(&args.db)?;
    let post = query_stats_post(&conn, &args.post_guid)?
        .ok_or_else(|| anyhow!("post not found for guid {}", args.post_guid))?;
    let mut segments = query_stats_transcript_segments(&conn, post.id)?;
    segments.sort_by_key(|s| s.sequence_num);

    let candidates = find_repeat_ad_candidates(
        &segments,
        args.target_first_seq,
        args.target_last_seq,
        &request.exclude_ranges,
        args.similarity_threshold,
    );

    let out: Vec<Value> = candidates
        .iter()
        .map(|c| {
            json!({
                "first_seq": c.first_seq,
                "last_seq": c.last_seq,
                "start_time": c.start_time,
                "end_time": c.end_time,
                "similarity": c.similarity,
            })
        })
        .collect();
    Ok(json!({ "candidates": out }))
}

/// Mirror of `WordBoundaryRefiner._get_context` in
/// `src/podcast_processor/word_boundary_refiner.py`. Prefer the explicit
/// seq-window when both first/last_seq are provided; otherwise fall back to
/// time-overlap selection with ±2 / +3 padding around the overlapping run.
fn select_wb_context_segments(
    segments: &[StatsTranscriptSegmentRow],
    ad_start: f64,
    ad_end: f64,
    first_seq: Option<i64>,
    last_seq: Option<i64>,
) -> Vec<&StatsTranscriptSegmentRow> {
    if let (Some(first), Some(last)) = (first_seq, last_seq) {
        let by_seq = wb_context_by_seq_window(segments, first, last);
        if !by_seq.is_empty() {
            return by_seq;
        }
    }
    wb_context_by_time_overlap(segments, ad_start, ad_end)
}

fn wb_context_by_seq_window(
    segments: &[StatsTranscriptSegmentRow],
    first_seq: i64,
    last_seq: i64,
) -> Vec<&StatsTranscriptSegmentRow> {
    if segments.is_empty() {
        return Vec::new();
    }
    let min_seq = segments.iter().map(|s| s.sequence_num).min().unwrap();
    let max_seq = segments.iter().map(|s| s.sequence_num).max().unwrap();
    let start_seq = min_seq.max(first_seq - 8);
    let end_seq = max_seq.min(last_seq + 8);
    segments
        .iter()
        .filter(|s| s.sequence_num >= start_seq && s.sequence_num <= end_seq)
        .collect()
}

fn wb_context_by_time_overlap(
    segments: &[StatsTranscriptSegmentRow],
    ad_start: f64,
    ad_end: f64,
) -> Vec<&StatsTranscriptSegmentRow> {
    let overlap_idxs: Vec<usize> = segments
        .iter()
        .enumerate()
        .filter(|(_, s)| s.start_time <= ad_end && s.end_time >= ad_start)
        .map(|(idx, _)| idx)
        .collect();
    if overlap_idxs.is_empty() {
        return Vec::new();
    }
    let first_idx = *overlap_idxs.first().unwrap();
    let last_idx = *overlap_idxs.last().unwrap();
    let start_idx = first_idx.saturating_sub(2);
    let end_idx = (last_idx + 3).min(segments.len());
    segments[start_idx..end_idx].iter().collect()
}

// === Word-boundary refiner: deterministic helpers ported from
// `src/podcast_processor/word_boundary_refiner.py`. The LLM call itself stays
// in Python; this is just the data-plumbing portion. Parity matters — these
// outputs drive audio cuts, so any divergence shows up as audible glitches.

const WB_MAX_START_EXTENSION_SECONDS: f64 = 30.0;
const WB_MAX_END_EXTENSION_SECONDS: f64 = 15.0;
const WB_PHRASE_MAX_WORDS: usize = 4;

#[derive(Clone)]
struct WbTimedWord {
    token: String,
    start: Option<f64>,
    end: Option<f64>,
}

fn wb_normalize_token(token: &str) -> String {
    // Mirror `_normalize_token`: strip leading/trailing chars that are not
    // alphanumeric or apostrophe; keep internal apostrophes.
    let is_keep = |c: char| c.is_ascii_alphanumeric() || c == '\'';
    let start = token.find(is_keep);
    if start.is_none() {
        return String::new();
    }
    let start = start.unwrap();
    // rfind returns byte index of the start of the matching char.
    let end_byte = token.rfind(is_keep).map(|i| {
        // Advance past the last kept char.
        let c = token[i..].chars().next().unwrap();
        i + c.len_utf8()
    });
    let end_byte = end_byte.unwrap_or(token.len());
    token[start..end_byte].to_string()
}

fn wb_split_words(text: &str) -> Vec<String> {
    text.split_whitespace()
        .map(wb_normalize_token)
        .filter(|t| !t.is_empty())
        .collect()
}

fn wb_split_words_lower(text: &str) -> Vec<String> {
    wb_split_words(text)
        .into_iter()
        .map(|t| t.to_lowercase())
        .collect()
}

fn wb_find_subsequence(
    words: &[String],
    target: &[String],
    choose_last: bool,
) -> Option<(usize, usize)> {
    if target.is_empty() || target.len() > words.len() {
        return None;
    }
    let k = target.len();
    let mut matches: Vec<(usize, usize)> = Vec::new();
    for i in 0..=(words.len() - k) {
        if &words[i..i + k] == target {
            matches.push((i, i + k - 1));
        }
    }
    if matches.is_empty() {
        None
    } else if choose_last {
        matches.last().copied()
    } else {
        matches.first().copied()
    }
}

/// Mirror of `_find_phrase_match`. For direction="start", search the first
/// `max_words` of the phrase, shrinking from the right; for direction="end",
/// search the last `max_words`, shrinking from the left.
fn wb_find_phrase_match(
    words: &[String],
    phrase_tokens: &[String],
    direction_end: bool,
    max_words: usize,
) -> Option<(usize, usize)> {
    if words.is_empty() || phrase_tokens.is_empty() {
        return None;
    }
    if !direction_end {
        let base_len = phrase_tokens.len().min(max_words);
        let base = &phrase_tokens[..base_len];
        for k in (1..=base_len).rev() {
            if let Some(m) = wb_find_subsequence(words, &base[..k], false) {
                return Some(m);
            }
        }
        None
    } else {
        let phrase_len = phrase_tokens.len();
        let base_start = phrase_len.saturating_sub(max_words);
        let base = &phrase_tokens[base_start..];
        for k in (1..=base.len()).rev() {
            let target = &base[base.len() - k..];
            if let Some(m) = wb_find_subsequence(words, target, true) {
                return Some(m);
            }
        }
        None
    }
}

fn wb_segment_word_entries(words_json: Option<&Value>) -> Vec<WbTimedWord> {
    let arr = match words_json {
        Some(Value::Array(items)) => items,
        _ => return Vec::new(),
    };
    let mut out = Vec::with_capacity(arr.len());
    for item in arr {
        let raw_token = item.get("word").and_then(|v| v.as_str()).unwrap_or("");
        let token = wb_normalize_token(raw_token).to_lowercase();
        if token.is_empty() {
            continue;
        }
        let start = item.get("start").and_then(|v| v.as_f64());
        let end = item.get("end").and_then(|v| v.as_f64());
        out.push(WbTimedWord { token, start, end });
    }
    out
}

fn wb_exact_phrase_boundary_time(
    timed_words: &[WbTimedWord],
    match_start_idx: usize,
    match_end_idx: usize,
    direction_end: bool,
) -> Option<f64> {
    if direction_end {
        timed_words.get(match_end_idx).and_then(|w| w.end)
    } else {
        timed_words.get(match_start_idx).and_then(|w| w.start)
    }
}

fn wb_resolve_word_index(
    words: &[String],
    word: Option<&str>,
    occurrence: Option<&str>,
    word_index: Option<i64>,
) -> usize {
    if let Some(raw) = word {
        let target = wb_normalize_token(raw.trim()).to_lowercase();
        if !target.is_empty() {
            let matches: Vec<usize> = words
                .iter()
                .enumerate()
                .filter_map(|(idx, w)| {
                    if w.to_lowercase() == target {
                        Some(idx)
                    } else {
                        None
                    }
                })
                .collect();
            if !matches.is_empty() {
                let occ = occurrence.unwrap_or("").trim().to_lowercase();
                return if occ == "last" {
                    *matches.last().unwrap()
                } else {
                    matches[0]
                };
            }
        }
    }
    let idx = word_index.unwrap_or(0);
    let clamped = idx.max(0).min((words.len() as i64).saturating_sub(1));
    clamped.max(0) as usize
}

#[derive(Clone)]
struct WbSegment {
    sequence_num: i64,
    start_time: f64,
    end_time: f64,
    text: String,
    /// Word-timestamp entries (may be empty if not provided by Whisper).
    words: Vec<Value>,
}

fn wb_find_segment(segments: &[WbSegment], seq: Option<i64>) -> Option<&WbSegment> {
    let seq = seq?;
    segments.iter().find(|s| s.sequence_num == seq)
}

fn wb_estimate_phrase_time_for_segment(
    segment: &WbSegment,
    phrase_tokens: &[String],
    direction_end: bool,
) -> Option<f64> {
    let timed_words = wb_segment_word_entries(Some(&Value::Array(segment.words.clone())));
    if !timed_words.is_empty() {
        let words: Vec<String> = timed_words.iter().map(|w| w.token.clone()).collect();
        if let Some((s_idx, e_idx)) =
            wb_find_phrase_match(&words, phrase_tokens, direction_end, WB_PHRASE_MAX_WORDS)
        {
            if let Some(t) =
                wb_exact_phrase_boundary_time(&timed_words, s_idx, e_idx, direction_end)
            {
                return Some(t);
            }
        }
    }

    let start_time = segment.start_time;
    let end_time = segment.end_time.max(start_time);
    let duration = (end_time - start_time).max(0.0);
    let words = wb_split_words_lower(&segment.text);
    if words.is_empty() || duration <= 0.0 {
        return None;
    }
    let (s_idx, e_idx) =
        wb_find_phrase_match(&words, phrase_tokens, direction_end, WB_PHRASE_MAX_WORDS)?;
    let seconds_per_word = duration / words.len() as f64;
    let estimated = if !direction_end {
        start_time + (s_idx as f64) * seconds_per_word
    } else {
        start_time + ((e_idx + 1) as f64) * seconds_per_word
    };
    Some(estimated.min(end_time))
}

fn wb_estimate_phrase_time(
    segments: &[WbSegment],
    context: &[&WbSegment],
    preferred_seq: Option<i64>,
    phrase: Option<&str>,
    direction_end: bool,
) -> Option<f64> {
    let phrase_text = phrase?;
    let tokens: Vec<String> = wb_split_words_lower(phrase_text);
    if tokens.is_empty() {
        return None;
    }

    let mut candidates: Vec<&WbSegment> = Vec::new();
    if let Some(preferred) = wb_find_segment(segments, preferred_seq) {
        candidates.push(preferred);
    }
    let mut ordered: Vec<&WbSegment> = context.to_vec();
    ordered.sort_by_key(|s| s.sequence_num);
    if direction_end {
        ordered.reverse();
    }
    for seg in ordered {
        if Some(seg.sequence_num) == preferred_seq {
            continue;
        }
        candidates.push(seg);
    }

    for seg in candidates {
        if let Some(t) = wb_estimate_phrase_time_for_segment(seg, &tokens, direction_end) {
            return Some(t);
        }
    }
    None
}

fn wb_estimate_word_time(
    segments: &[WbSegment],
    segment_seq: Option<i64>,
    word: Option<&str>,
    occurrence: Option<&str>,
    word_index: Option<i64>,
) -> f64 {
    let seg = match wb_find_segment(segments, segment_seq) {
        Some(s) => s,
        None => return segments.first().map(|s| s.start_time).unwrap_or(0.0),
    };
    let start_time = seg.start_time;
    let end_time = seg.end_time.max(start_time);
    let duration = (end_time - start_time).max(0.0);

    let timed_words = wb_segment_word_entries(Some(&Value::Array(seg.words.clone())));
    if !timed_words.is_empty() {
        let words: Vec<String> = timed_words.iter().map(|w| w.token.clone()).collect();
        let idx = wb_resolve_word_index(&words, word, occurrence, word_index);
        if let Some(exact) = timed_words[idx].start {
            return exact.min(end_time);
        }
    }
    let words = wb_split_words(&seg.text);
    if words.is_empty() || duration <= 0.0 {
        return start_time;
    }
    let idx = wb_resolve_word_index(&words, word, occurrence, word_index);
    let seconds_per_word = duration / words.len() as f64;
    (start_time + (idx as f64) * seconds_per_word).min(end_time)
}

fn wb_estimate_segment_boundary_time(
    segments: &[WbSegment],
    segment_seq: Option<i64>,
    boundary_end: bool,
) -> Option<f64> {
    let seg = wb_find_segment(segments, segment_seq)?;
    Some(if boundary_end {
        seg.end_time
    } else {
        seg.start_time
    })
}

fn wb_constrain_start(estimated: f64, orig: f64) -> f64 {
    estimated.max(orig - WB_MAX_START_EXTENSION_SECONDS)
}

fn wb_constrain_end(estimated: f64, orig: f64) -> f64 {
    estimated.min(orig + WB_MAX_END_EXTENSION_SECONDS)
}

fn wb_has_text(s: Option<&str>) -> bool {
    s.map(|t| !t.trim().is_empty()).unwrap_or(false)
}

#[allow(clippy::too_many_arguments)]
fn wb_refine_start(
    segments: &[WbSegment],
    context: &[&WbSegment],
    ad_start: f64,
    start_segment_seq: Option<i64>,
    start_phrase: Option<&str>,
    start_word: Option<&str>,
    start_occurrence: Option<&str>,
    start_word_index: Option<i64>,
) -> (f64, bool, Option<String>) {
    if wb_has_text(start_phrase) {
        match wb_estimate_phrase_time(segments, context, start_segment_seq, start_phrase, false) {
            Some(est) => (wb_constrain_start(est, ad_start), true, None),
            None => (ad_start, false, Some("start_phrase_not_found".to_string())),
        }
    } else if let Some(seg_start) =
        wb_estimate_segment_boundary_time(segments, start_segment_seq, false)
    {
        let constrained = wb_constrain_start(seg_start, ad_start);
        (constrained, constrained != ad_start, None)
    } else if wb_has_text(start_word) || start_word_index.is_some() {
        let est = wb_estimate_word_time(
            segments,
            start_segment_seq,
            start_word,
            start_occurrence,
            start_word_index,
        );
        (wb_constrain_start(est, ad_start), true, None)
    } else {
        (ad_start, false, None)
    }
}

fn wb_refine_end(
    segments: &[WbSegment],
    context: &[&WbSegment],
    ad_end: f64,
    end_segment_seq: Option<i64>,
    end_phrase: Option<&str>,
) -> (f64, bool, Option<String>) {
    if !wb_has_text(end_phrase) {
        if let Some(seg_end) = wb_estimate_segment_boundary_time(segments, end_segment_seq, true) {
            let constrained = wb_constrain_end(seg_end, ad_end);
            return (constrained, constrained != ad_end, None);
        }
        return (ad_end, false, None);
    }
    match wb_estimate_phrase_time(segments, context, end_segment_seq, end_phrase, true) {
        Some(est) => (wb_constrain_end(est, ad_end), true, None),
        None => (ad_end, false, Some("end_phrase_not_found".to_string())),
    }
}

fn run_transcript_wb_resolve(args: TranscriptWbResolveArgs) -> Result<Value> {
    let conn = open_readonly_sqlite(&args.db)?;
    let post = query_stats_post(&conn, &args.post_guid)?
        .ok_or_else(|| anyhow!("post not found for guid {}", args.post_guid))?;
    let segment_rows = query_stats_transcript_segments(&conn, post.id)?;
    let words_by_seq = query_wb_word_timestamps_by_seq(&conn, post.id)?;

    let segments: Vec<WbSegment> = segment_rows
        .into_iter()
        .map(|row| {
            let words = words_by_seq
                .get(&row.sequence_num)
                .cloned()
                .unwrap_or_default();
            WbSegment {
                sequence_num: row.sequence_num,
                start_time: row.start_time,
                end_time: row.end_time,
                text: row.text,
                words,
            }
        })
        .collect();

    let context_refs = select_wb_context_segments_wb(
        &segments,
        args.orig_ad_start,
        args.orig_ad_end,
        args.first_seq,
        args.last_seq,
    );

    let (refined_start, start_changed, start_error) = wb_refine_start(
        &segments,
        &context_refs,
        args.orig_ad_start,
        args.start_segment_seq,
        args.start_phrase.as_deref(),
        args.start_word.as_deref(),
        args.start_occurrence.as_deref(),
        args.start_word_index,
    );

    let (refined_end, end_changed, end_error) = wb_refine_end(
        &segments,
        &context_refs,
        args.orig_ad_end,
        args.end_segment_seq,
        args.end_phrase.as_deref(),
    );

    Ok(json!({
        "refined_start": refined_start,
        "refined_end": refined_end,
        "start_changed": start_changed,
        "end_changed": end_changed,
        "start_error": start_error,
        "end_error": end_error,
    }))
}

// `select_wb_context_segments_wb` (defined below) is the `WbSegment` variant
// of `select_wb_context_segments`, used by callers that already loaded the
// word-timestamp arrays and don't want to re-shape rows.
//
// === Word-boundary JSON-repair helpers ported from
// `_parse_json` / `_parse_json_candidate` / `_repair_truncated_json` /
// `_parse_partial_json_fields` / `_json_parse_candidates` /
// `_extract_json_objects` in `word_boundary_refiner.py`. The port replicates
// the same best-effort recovery chain so a single Rust subprocess call can
// fully replace the Python `_parse_json` method on the LLM response.

fn wb_strip_code_fences(text: &str) -> String {
    // Mirrors `re.sub(r"```(?:json)?|```", "", text, flags=re.IGNORECASE)`.
    let pat = regex::Regex::new(r"(?i)```(?:json)?|```").unwrap();
    pat.replace_all(text, "").into_owned()
}

fn wb_dedupe_preserve_order(values: Vec<String>) -> Vec<String> {
    let mut out: Vec<String> = Vec::with_capacity(values.len());
    let mut seen: std::collections::HashSet<String> = std::collections::HashSet::new();
    for value in values {
        let trimmed = value.trim().to_string();
        if trimmed.is_empty() || !seen.insert(trimmed.clone()) {
            continue;
        }
        out.push(trimmed);
    }
    out
}

/// Mirror of `_extract_json_objects`: walks `text` byte-by-byte, tracking
/// brace depth and string state, and emits each top-level `{...}` substring.
fn wb_extract_json_objects(text: &str) -> Vec<String> {
    let bytes = text.as_bytes();
    let mut objects: Vec<String> = Vec::new();
    let mut depth: i32 = 0;
    let mut start_idx: Option<usize> = None;
    let mut in_string = false;
    let mut escaped = false;

    let mut idx = 0;
    while idx < bytes.len() {
        let c = bytes[idx];
        if in_string {
            if escaped {
                escaped = false;
            } else if c == b'\\' {
                escaped = true;
            } else if c == b'"' {
                in_string = false;
            }
            idx += 1;
            continue;
        }

        if c == b'"' {
            in_string = true;
            idx += 1;
            continue;
        }

        if c == b'{' {
            if depth == 0 {
                start_idx = Some(idx);
            }
            depth += 1;
            idx += 1;
            continue;
        }

        if c == b'}' && depth > 0 {
            depth -= 1;
            if depth == 0 {
                if let Some(s) = start_idx {
                    // Safe because we only ever indexed at ASCII brace/quote
                    // boundaries; the slice between is a valid UTF-8 substring.
                    if let Some(slice) = text.get(s..=idx) {
                        objects.push(slice.to_string());
                    }
                    start_idx = None;
                }
            }
        }
        idx += 1;
    }
    objects
}

fn wb_json_parse_candidates(content: &str) -> Vec<String> {
    let text = content.trim();
    if text.is_empty() {
        return Vec::new();
    }

    let mut candidates: Vec<String> = vec![text.to_string()];

    // Extract everything between ```json ... ``` (or plain ``` ... ```).
    let fence_pat = regex::Regex::new(r"(?is)```(?:json)?\s*(.*?)```").unwrap();
    for caps in fence_pat.captures_iter(text) {
        if let Some(m) = caps.get(1) {
            let block = m.as_str().trim();
            if !block.is_empty() {
                candidates.push(block.to_string());
            }
        }
    }

    let unfenced = wb_strip_code_fences(text);
    let unfenced = unfenced.trim();
    if !unfenced.is_empty() {
        candidates.push(unfenced.to_string());
    }

    let mut expanded: Vec<String> = Vec::with_capacity(candidates.len() * 2);
    for candidate in candidates {
        expanded.push(candidate.clone());
        expanded.extend(wb_extract_json_objects(&candidate));
    }
    wb_dedupe_preserve_order(expanded)
}

/// Mirror of `_repair_truncated_json`. Returns the repaired candidate or
/// `None` when the input has no usable structure to recover.
fn wb_repair_truncated_json(candidate: &str) -> Option<String> {
    let text = candidate.trim();
    if text.is_empty() {
        return None;
    }
    let start_idx = text.find('{')?;
    let mut repaired = text[start_idx..].to_string();

    repaired = wb_strip_code_fences(&repaired).trim().to_string();
    // Strip trailing commas (matches Python `repaired.rstrip(",")`).
    repaired = repaired.trim_end_matches(',').to_string();

    // Drop an obviously incomplete trailing key/value pair if present.
    for pat_str in [
        r#",\s*"[^"]*$"#,
        r#",\s*"[^"]*"$"#,
        r#",\s*"[^"]*"\s*:\s*$"#,
        r#",\s*"[^"]*"\s*:\s*"[^"]*$"#,
    ] {
        let pat = regex::Regex::new(pat_str).unwrap();
        repaired = pat.replace(&repaired, "").into_owned();
    }

    let open_brackets = repaired.matches('[').count();
    let close_brackets = repaired.matches(']').count();
    if close_brackets < open_brackets {
        repaired.push_str(&"]".repeat(open_brackets - close_brackets));
    }
    let open_braces = repaired.matches('{').count();
    let close_braces = repaired.matches('}').count();
    if close_braces < open_braces {
        repaired.push_str(&"}".repeat(open_braces - close_braces));
    }

    if repaired.is_empty() {
        None
    } else {
        Some(repaired)
    }
}

fn wb_parse_json_candidate(candidate: &str) -> Option<Value> {
    let mut attempts: Vec<String> = vec![candidate.to_string()];
    if let Some(repaired) = wb_repair_truncated_json(candidate) {
        if repaired != candidate {
            attempts.push(repaired);
        }
    }
    for attempt in attempts {
        match serde_json::from_str::<Value>(&attempt) {
            Ok(v) if v.is_object() => return Some(v),
            _ => continue,
        }
    }
    None
}

/// Mirror of `_parse_partial_json_fields`. Returns the partial object that
/// could be regex-extracted from a malformed LLM response.
fn wb_parse_partial_json_fields(content: &str) -> Value {
    let mut out = serde_json::Map::new();

    let extract_int_or_null = |key: &str| -> (Option<Option<i64>>, bool) {
        // Returns:
        //   (Some(Some(N)), _) — saw a numeric value
        //   (Some(None), true) — saw an explicit `"key": null`
        //   (None, _) — not present
        let escaped = regex::escape(key);
        let pat = regex::Regex::new(&format!(r#"(?i)"{escaped}"\s*:\s*(null|-?\d+)"#)).unwrap();
        let null_pat = regex::Regex::new(&format!(r#"(?i)"{escaped}"\s*:\s*null"#)).unwrap();
        let null_present = null_pat.is_match(content);
        if let Some(caps) = pat.captures(content) {
            let raw = caps.get(1).unwrap().as_str();
            if raw.eq_ignore_ascii_case("null") {
                (Some(None), null_present)
            } else {
                match raw.parse::<i64>() {
                    Ok(n) => (Some(Some(n)), null_present),
                    Err(_) => (None, null_present),
                }
            }
        } else {
            (None, null_present)
        }
    };

    let extract_string = |key: &str| -> Option<String> {
        let escaped = regex::escape(key);
        let pat = regex::Regex::new(&format!(r#"(?is)"{escaped}"\s*:\s*"([^"]*)""#)).unwrap();
        let caps = pat.captures(content)?;
        let raw = caps.get(1).unwrap().as_str().trim();
        if raw.is_empty() {
            None
        } else {
            Some(raw.to_string())
        }
    };

    // Note: Python emits keys for both int matches *and* explicit null, so the
    // downstream `_extract_payload` sees `None` instead of falling through.
    let (start_seq, start_seq_null) = extract_int_or_null("refined_start_segment_seq");
    let (end_seq, end_seq_null) = extract_int_or_null("refined_end_segment_seq");
    if let Some(value) = start_seq {
        out.insert(
            "refined_start_segment_seq".into(),
            match value {
                Some(n) => Value::from(n),
                None => Value::Null,
            },
        );
    } else if start_seq_null {
        out.insert("refined_start_segment_seq".into(), Value::Null);
    }
    if let Some(value) = end_seq {
        out.insert(
            "refined_end_segment_seq".into(),
            match value {
                Some(n) => Value::from(n),
                None => Value::Null,
            },
        );
    } else if end_seq_null {
        out.insert("refined_end_segment_seq".into(), Value::Null);
    }
    if let Some(s) = extract_string("refined_start_phrase") {
        out.insert("refined_start_phrase".into(), Value::String(s));
    }
    if let Some(s) = extract_string("refined_end_phrase") {
        out.insert("refined_end_phrase".into(), Value::String(s));
    }
    if let Some(s) = extract_string("start_adjustment_reason") {
        out.insert("start_adjustment_reason".into(), Value::String(s));
    }
    if let Some(s) = extract_string("end_adjustment_reason") {
        out.insert("end_adjustment_reason".into(), Value::String(s));
    }

    Value::Object(out)
}

/// End-to-end mirror of `WordBoundaryRefiner._parse_json`. Returns
/// `(parsed_object, salvaged_from_partial_fields)`.
fn wb_parse_json_full(content: &str) -> (Option<Value>, bool) {
    for candidate in wb_json_parse_candidates(content) {
        if let Some(parsed) = wb_parse_json_candidate(&candidate) {
            return (Some(parsed), false);
        }
    }
    let partial = wb_parse_partial_json_fields(content);
    if let Value::Object(ref map) = partial {
        if !map.is_empty() {
            return (Some(partial), true);
        }
    }
    (None, false)
}

fn run_transcript_wb_json_parse(args: TranscriptWbJsonParseArgs) -> Result<Value> {
    let content = std::fs::read_to_string(&args.input).with_context(|| {
        format!(
            "failed to read wb-json-parse input {}",
            args.input.display()
        )
    })?;
    let (parsed, salvaged) = wb_parse_json_full(&content);
    Ok(json!({
        "parsed": parsed,
        "salvaged": salvaged,
    }))
}

/// In-memory mirror of `WordBoundaryRefiner._extract_payload`. Extracted as a
/// struct so the bundled `wb-refine-from-llm` flow can pass the fields straight
/// into the existing `wb_refine_start` / `wb_refine_end` helpers without
/// stringifying through CLI arguments.
struct WbExtractedPayload {
    start_segment_seq: Option<i64>,
    start_phrase: Option<String>,
    end_segment_seq: Option<i64>,
    end_phrase: Option<String>,
    start_word: Option<String>,
    start_occurrence: Option<String>,
    start_word_index: Option<i64>,
    start_reason: String,
    end_reason: String,
}

fn wb_coerce_int(value: Option<&Value>) -> Option<i64> {
    let v = value?;
    if v.is_null() {
        return None;
    }
    if let Some(n) = v.as_i64() {
        return Some(n);
    }
    if let Some(f) = v.as_f64() {
        if f.is_finite() {
            return Some(f as i64);
        }
    }
    if let Some(s) = v.as_str() {
        if let Ok(n) = s.trim().parse::<i64>() {
            return Some(n);
        }
        if let Ok(f) = s.trim().parse::<f64>() {
            if f.is_finite() {
                return Some(f as i64);
            }
        }
    }
    None
}

fn wb_coerce_optional_str(value: Option<&Value>) -> Option<String> {
    let v = value?;
    let s = v.as_str()?;
    let trimmed = s.trim();
    if trimmed.is_empty() {
        None
    } else {
        Some(trimmed.to_string())
    }
}

fn wb_coerce_reason_str(value: Option<&Value>) -> String {
    match value {
        Some(v) if v.is_string() => v.as_str().unwrap_or("").to_string(),
        _ => String::new(),
    }
}

fn wb_extract_payload(parsed: &Value) -> WbExtractedPayload {
    let occurrence = parsed.get("occurrence").or_else(|| parsed.get("occurance"));
    WbExtractedPayload {
        start_segment_seq: wb_coerce_int(parsed.get("refined_start_segment_seq")),
        start_phrase: wb_coerce_optional_str(parsed.get("refined_start_phrase")),
        end_segment_seq: wb_coerce_int(parsed.get("refined_end_segment_seq")),
        end_phrase: wb_coerce_optional_str(parsed.get("refined_end_phrase")),
        start_word: wb_coerce_optional_str(parsed.get("refined_start_word")),
        start_occurrence: wb_coerce_optional_str(occurrence),
        start_word_index: wb_coerce_int(parsed.get("refined_start_word_index")),
        start_reason: wb_coerce_reason_str(parsed.get("start_adjustment_reason")),
        end_reason: wb_coerce_reason_str(parsed.get("end_adjustment_reason")),
    }
}

// Bundled `parse → context → resolve` core, factored out so unit tests can
// drive it without seeding SQLite. The production DB-backed subcommand
// re-implements the same flow inline so it can prune the `transcript_word_timestamps`
// JSON load down to just the segments that matter; this helper accepts a
// pre-built `&[WbSegment]` list (words already attached, if any) and is
// retained for the in-memory unit tests below.
#[cfg(test)]
fn wb_refine_from_llm_inner(
    segments: &[WbSegment],
    content: &str,
    orig_ad_start: f64,
    orig_ad_end: f64,
    first_seq: Option<i64>,
    last_seq: Option<i64>,
) -> Value {
    let (parsed_opt, salvaged) = wb_parse_json_full(content);
    let parsed = match parsed_opt {
        Some(p) => p,
        None => {
            return json!({
                "parse_status": "failed",
                "refined_start": orig_ad_start,
                "refined_end": orig_ad_end,
                "start_changed": false,
                "end_changed": false,
                "start_error": Value::Null,
                "end_error": Value::Null,
                "start_reason": "",
                "end_reason": "",
            });
        }
    };
    let payload = wb_extract_payload(&parsed);
    let context_refs =
        select_wb_context_segments_wb(segments, orig_ad_start, orig_ad_end, first_seq, last_seq);
    let (refined_start, start_changed, start_error) = wb_refine_start(
        segments,
        &context_refs,
        orig_ad_start,
        payload.start_segment_seq,
        payload.start_phrase.as_deref(),
        payload.start_word.as_deref(),
        payload.start_occurrence.as_deref(),
        payload.start_word_index,
    );
    let (refined_end, end_changed, end_error) = wb_refine_end(
        segments,
        &context_refs,
        orig_ad_end,
        payload.end_segment_seq,
        payload.end_phrase.as_deref(),
    );
    json!({
        "parse_status": if salvaged { "salvaged" } else { "ok" },
        "refined_start": refined_start,
        "refined_end": refined_end,
        "start_changed": start_changed,
        "end_changed": end_changed,
        "start_error": start_error,
        "end_error": end_error,
        "start_reason": payload.start_reason,
        "end_reason": payload.end_reason,
    })
}

fn run_transcript_wb_refine_from_llm(args: TranscriptWbRefineFromLlmArgs) -> Result<Value> {
    let content = std::fs::read_to_string(&args.raw_content_file).with_context(|| {
        format!(
            "failed to read wb-refine-from-llm input {}",
            args.raw_content_file.display()
        )
    })?;

    // Parse the LLM JSON first. If it fails entirely there is no payload, no
    // need to touch SQLite or the megabyte `transcript_word_timestamps` blob.
    let (parsed_opt, salvaged) = wb_parse_json_full(&content);
    let parsed = match parsed_opt {
        Some(p) => p,
        None => {
            return Ok(json!({
                "parse_status": "failed",
                "refined_start": args.orig_ad_start,
                "refined_end": args.orig_ad_end,
                "start_changed": false,
                "end_changed": false,
                "start_error": Value::Null,
                "end_error": Value::Null,
                "start_reason": "",
                "end_reason": "",
            }));
        }
    };
    let payload = wb_extract_payload(&parsed);

    let conn = open_readonly_sqlite(&args.db)?;
    let post = query_stats_post(&conn, &args.post_guid)?
        .ok_or_else(|| anyhow!("post not found for guid {}", args.post_guid))?;
    // Range-filter the segment query when we have a seq window. Loading 20
    // rows instead of 2000+ saves several ms on long episodes; production
    // always supplies first_seq/last_seq from the detected ad block, so this
    // is the hot path. Fall back to the full fetch only when the caller
    // didn't supply a window (matches the time-overlap context fallback).
    let segment_rows = match (args.first_seq, args.last_seq) {
        (Some(f), Some(l)) => {
            let lo = f.min(l);
            let hi = f.max(l);
            // ±2 mirrors `select_wb_context_segments_wb`; the extra ±2 buffer
            // on top of that handles LLM payloads whose `refined_*_segment_seq`
            // lands just outside the detected window.
            let mut lo_pad = lo - 4;
            let mut hi_pad = hi + 4;
            if let Some(s) = payload.start_segment_seq {
                lo_pad = lo_pad.min(s);
                hi_pad = hi_pad.max(s);
            }
            if let Some(s) = payload.end_segment_seq {
                lo_pad = lo_pad.min(s);
                hi_pad = hi_pad.max(s);
            }
            query_stats_transcript_segments_in_seq_range(&conn, post.id, lo_pad, hi_pad)?
        }
        _ => query_stats_transcript_segments(&conn, post.id)?,
    };

    // Build segments without word arrays first so the context selector can
    // run on bare seq/start/end metadata. We only attach the parsed `words`
    // vectors below for the small set of segments that the resolver actually
    // looks at — preferred start/end + the ±2 seq context window. On posts
    // with a multi-MB `transcript_word_timestamps` blob this turns the
    // per-call cost from "parse the whole JSON" into "parse a handful of
    // word arrays".
    let mut segments: Vec<WbSegment> = segment_rows
        .into_iter()
        .map(|row| WbSegment {
            sequence_num: row.sequence_num,
            start_time: row.start_time,
            end_time: row.end_time,
            text: row.text,
            words: Vec::new(),
        })
        .collect();

    let context_refs = select_wb_context_segments_wb(
        &segments,
        args.orig_ad_start,
        args.orig_ad_end,
        args.first_seq,
        args.last_seq,
    );
    let mut wanted: std::collections::HashSet<i64> =
        context_refs.iter().map(|s| s.sequence_num).collect();
    if let Some(seq) = payload.start_segment_seq {
        wanted.insert(seq);
    }
    if let Some(seq) = payload.end_segment_seq {
        wanted.insert(seq);
    }
    drop(context_refs);

    // Skip the multi-MB JSON column entirely when the LLM didn't ask for
    // phrase/word resolution. `wb_refine_start`/`wb_refine_end` fall back to
    // pure segment-boundary times in that case, which don't need
    // `transcript_word_timestamps` at all.
    let needs_words = wb_has_text(payload.start_phrase.as_deref())
        || wb_has_text(payload.end_phrase.as_deref())
        || wb_has_text(payload.start_word.as_deref())
        || payload.start_word_index.is_some();
    let words_by_seq = if needs_words {
        query_wb_word_timestamps_for_seqs(&conn, post.id, &wanted)?
    } else {
        HashMap::new()
    };
    for seg in &mut segments {
        if let Some(words) = words_by_seq.get(&seg.sequence_num) {
            seg.words = words.clone();
        }
    }

    let context_refs = select_wb_context_segments_wb(
        &segments,
        args.orig_ad_start,
        args.orig_ad_end,
        args.first_seq,
        args.last_seq,
    );

    let (refined_start, start_changed, start_error) = wb_refine_start(
        &segments,
        &context_refs,
        args.orig_ad_start,
        payload.start_segment_seq,
        payload.start_phrase.as_deref(),
        payload.start_word.as_deref(),
        payload.start_occurrence.as_deref(),
        payload.start_word_index,
    );
    let (refined_end, end_changed, end_error) = wb_refine_end(
        &segments,
        &context_refs,
        args.orig_ad_end,
        payload.end_segment_seq,
        payload.end_phrase.as_deref(),
    );

    Ok(json!({
        "parse_status": if salvaged { "salvaged" } else { "ok" },
        "refined_start": refined_start,
        "refined_end": refined_end,
        "start_changed": start_changed,
        "end_changed": end_changed,
        "start_error": start_error,
        "end_error": end_error,
        "start_reason": payload.start_reason,
        "end_reason": payload.end_reason,
    }))
}

fn select_wb_context_segments_wb(
    segments: &[WbSegment],
    ad_start: f64,
    ad_end: f64,
    first_seq: Option<i64>,
    last_seq: Option<i64>,
) -> Vec<&WbSegment> {
    if let (Some(first), Some(last)) = (first_seq, last_seq) {
        if !segments.is_empty() {
            let min_seq = segments.iter().map(|s| s.sequence_num).min().unwrap();
            let max_seq = segments.iter().map(|s| s.sequence_num).max().unwrap();
            let start_seq = min_seq.max(first - 2);
            let end_seq = max_seq.min(last + 2);
            let selected: Vec<&WbSegment> = segments
                .iter()
                .filter(|s| s.sequence_num >= start_seq && s.sequence_num <= end_seq)
                .collect();
            if !selected.is_empty() {
                return selected;
            }
        }
    }
    let overlap_idxs: Vec<usize> = segments
        .iter()
        .enumerate()
        .filter(|(_, s)| s.start_time <= ad_end && s.end_time >= ad_start)
        .map(|(idx, _)| idx)
        .collect();
    if overlap_idxs.is_empty() {
        return Vec::new();
    }
    let first_idx = *overlap_idxs.first().unwrap();
    let last_idx = *overlap_idxs.last().unwrap();
    let start_idx = first_idx.saturating_sub(2);
    let end_idx = (last_idx + 3).min(segments.len());
    segments[start_idx..end_idx].iter().collect()
}

/// Read `post.transcript_word_timestamps` (TEXT JSON) and return a per-segment
/// map keyed by sequence_num. The column is a JSON array of
/// `{"sequence_num": N, "words": [{"word": ..., "start": ..., "end": ..., "score": ...}, ...]}`.
fn query_wb_word_timestamps_by_seq(
    conn: &Connection,
    post_id: i64,
) -> Result<HashMap<i64, Vec<Value>>> {
    let mut stmt =
        conn.prepare("SELECT transcript_word_timestamps FROM post WHERE id = ?1 LIMIT 1")?;
    let raw: Option<String> = stmt
        .query_row([post_id], |row| row.get::<_, Option<String>>(0))
        .unwrap_or(None);
    let Some(raw) = raw else {
        return Ok(HashMap::new());
    };
    let parsed: Value = match serde_json::from_str(&raw) {
        Ok(v) => v,
        Err(_) => return Ok(HashMap::new()),
    };
    let Some(arr) = parsed.as_array() else {
        return Ok(HashMap::new());
    };
    let mut map: HashMap<i64, Vec<Value>> = HashMap::new();
    for entry in arr {
        let seq = match entry.get("sequence_num").and_then(|v| v.as_i64()) {
            Some(s) => s,
            None => continue,
        };
        let words = match entry.get("words") {
            Some(Value::Array(items)) => items.clone(),
            _ => continue,
        };
        if !words.is_empty() {
            map.insert(seq, words);
        }
    }
    Ok(map)
}

/// Lazy variant of `query_wb_word_timestamps_by_seq`: only materializes the
/// `Vec<Value>` for the seq numbers in `wanted`. The outer JSON array is
/// scanned to find each entry's byte boundaries via
/// `serde_json::value::RawValue`, but the word array itself is only parsed
/// for entries we actually intend to use. This is the hot path for the
/// bundled `wb-refine-from-llm` subcommand on posts with multi-megabyte
/// `transcript_word_timestamps` blobs.
#[derive(serde::Deserialize)]
struct WbRawWtEntry {
    sequence_num: i64,
    #[serde(default)]
    words: Option<Box<serde_json::value::RawValue>>,
}

fn query_wb_word_timestamps_for_seqs(
    conn: &Connection,
    post_id: i64,
    wanted: &std::collections::HashSet<i64>,
) -> Result<HashMap<i64, Vec<Value>>> {
    if wanted.is_empty() {
        return Ok(HashMap::new());
    }
    let mut stmt =
        conn.prepare("SELECT transcript_word_timestamps FROM post WHERE id = ?1 LIMIT 1")?;
    let raw: Option<String> = stmt
        .query_row([post_id], |row| row.get::<_, Option<String>>(0))
        .unwrap_or(None);
    let Some(raw) = raw else {
        return Ok(HashMap::new());
    };
    let entries: Vec<WbRawWtEntry> = match serde_json::from_str(&raw) {
        Ok(v) => v,
        Err(_) => return Ok(HashMap::new()),
    };
    let mut map: HashMap<i64, Vec<Value>> = HashMap::new();
    for entry in entries {
        if !wanted.contains(&entry.sequence_num) {
            continue;
        }
        let Some(raw_words) = entry.words else {
            continue;
        };
        match serde_json::from_str::<Vec<Value>>(raw_words.get()) {
            Ok(words) if !words.is_empty() => {
                map.insert(entry.sequence_num, words);
            }
            _ => continue,
        }
    }
    Ok(map)
}

// === Chapter fallback: topic-block builder ported from
// `src/podcast_processor/chapter_fallback.py:_build_topic_blocks`. Goal is
// byte-for-byte parity with the Python implementation so retry block counts
// match. The LLM call itself stays in Python.

fn chapter_seg_start_ms(start_time: f64) -> i64 {
    (start_time * 1000.0) as i64
}

fn chapter_seg_end_ms(end_time: f64) -> i64 {
    (end_time * 1000.0) as i64
}

fn chapter_transcript_duration_ms(segments: &[StatsTranscriptSegmentRow]) -> Option<i64> {
    if segments.is_empty() {
        return None;
    }
    let max_end_ms = segments
        .iter()
        .map(|s| chapter_seg_end_ms(s.end_time))
        .max()?;
    if max_end_ms > 0 {
        Some(max_end_ms)
    } else {
        None
    }
}

fn chapter_format_timestamp(ms: i64) -> String {
    let total_seconds = ms.max(0) / 1000;
    let hours = total_seconds / 3600;
    let rem = total_seconds % 3600;
    let minutes = rem / 60;
    let seconds = rem % 60;
    if hours > 0 {
        format!("{hours}:{minutes:02}:{seconds:02}")
    } else {
        format!("{minutes:02}:{seconds:02}")
    }
}

/// Collapse runs of whitespace to a single space and trim. Mirrors
/// `re.sub(r"\s+", " ", text).strip()` for ASCII-and-newlines inputs.
fn chapter_normalize_whitespace(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    let mut last_was_space = false;
    for c in text.chars() {
        if c.is_whitespace() {
            if !last_was_space {
                out.push(' ');
                last_was_space = true;
            }
        } else {
            out.push(c);
            last_was_space = false;
        }
    }
    out.trim().to_string()
}

#[derive(Clone)]
struct ChapterTopicBlock {
    start_ms: i64,
    end_ms: i64,
    text_parts: Vec<String>,
    #[allow(dead_code)]
    char_count: usize,
}

fn chapter_build_topic_blocks(
    segments: &[StatsTranscriptSegmentRow],
    total_duration_ms: i64,
    target_block_count: i64,
    min_block_seconds: i64,
    max_block_seconds: i64,
    max_chars_per_block: i64,
    include_segment_markers: bool,
) -> Vec<Value> {
    if segments.is_empty() {
        return Vec::new();
    }

    let raw_window_ms = (total_duration_ms.max(1)) / target_block_count.max(1);
    let raw_window_ms = raw_window_ms.max(1);
    let min_block_ms = (min_block_seconds * 1000).max(1);
    let max_block_ms = (max_block_seconds * 1000).max(min_block_ms);
    let mut block_window_ms = max_block_ms.min(min_block_ms.max(raw_window_ms));
    // Round up to the nearest 30s for cleaner boundaries.
    let round_ms: i64 = 30_000;
    block_window_ms = round_ms.max(((block_window_ms + round_ms - 1) / round_ms) * round_ms);
    block_window_ms = max_block_ms.min(block_window_ms);

    let max_chars = max_chars_per_block.max(0) as usize;

    let mut blocks: Vec<ChapterTopicBlock> = Vec::new();
    let mut current: Option<ChapterTopicBlock> = None;

    let flush = |current: &mut Option<ChapterTopicBlock>, blocks: &mut Vec<ChapterTopicBlock>| {
        if let Some(b) = current.take() {
            blocks.push(b);
        }
    };

    for seg in segments {
        let seg_start_ms = chapter_seg_start_ms(seg.start_time);
        let seg_end_ms = chapter_seg_end_ms(seg.end_time).max(seg_start_ms);
        let seg_text = chapter_normalize_whitespace(&seg.text);

        if let Some(ref mut block) = current {
            if seg_start_ms - block.start_ms >= block_window_ms {
                flush(&mut current, &mut blocks);
                current = Some(ChapterTopicBlock {
                    start_ms: seg_start_ms,
                    end_ms: seg_end_ms,
                    text_parts: Vec::new(),
                    char_count: 0,
                });
            } else {
                block.end_ms = block.end_ms.max(seg_end_ms);
            }
        } else {
            current = Some(ChapterTopicBlock {
                start_ms: seg_start_ms,
                end_ms: seg_end_ms,
                text_parts: Vec::new(),
                char_count: 0,
            });
        }

        if seg_text.is_empty() {
            continue;
        }
        // Accumulate the full segment text per block; truncation happens at
        // flush time so we can take a head slice AND a middle slice (see
        // chapter_truncate_block_text). This mirrors the Python helper in
        // src/podcast_processor/chapter_fallback.py::_truncate_block_text.
        let block = current.as_mut().unwrap();
        if include_segment_markers {
            block
                .text_parts
                .push(format!("[s{}] {}", seg.sequence_num, seg_text));
        } else {
            block.text_parts.push(seg_text);
        }
    }

    flush(&mut current, &mut blocks);

    // Mirror Python's "remove empty blocks when possible; keep at least one".
    let mut out: Vec<Value> = Vec::with_capacity(blocks.len());
    let mut nonempty_indices: Vec<usize> = Vec::with_capacity(blocks.len());
    for (idx, b) in blocks.iter().enumerate() {
        let text = b.text_parts.join(" ").trim().to_string();
        if !text.is_empty() {
            nonempty_indices.push(idx);
        }
    }
    let keep_indices: Vec<usize> = if nonempty_indices.is_empty() {
        if blocks.is_empty() {
            Vec::new()
        } else {
            vec![0]
        }
    } else {
        nonempty_indices
    };

    for (new_idx, &orig_idx) in keep_indices.iter().enumerate() {
        let b = &blocks[orig_idx];
        let text = if include_segment_markers {
            // Part-granular truncation keeps every [sNNN] marker intact so the
            // LLM can name any surviving segment as a chapter start anchor.
            chapter_truncate_block_parts(&b.text_parts, max_chars)
        } else {
            let joined = b.text_parts.join(" ").trim().to_string();
            chapter_truncate_block_text(&joined, max_chars)
        };
        out.push(json!({
            "block_index": new_idx as i64,
            "start_ms": b.start_ms,
            "end_ms": b.end_ms.max(b.start_ms),
            "timestamp": chapter_format_timestamp(b.start_ms),
            "text": text,
        }));
    }
    out
}

/// Part-granular variant of `chapter_truncate_block_text` for marker mode.
/// Keeps whole per-segment parts (so inline `[sNNN]` markers are never cut
/// mid-token): a run from the block head plus a run around the midpoint,
/// joined by " ... ". `max_chars == 0` means no truncation. Mirrors
/// `_truncate_block_parts` in `src/podcast_processor/chapter_fallback.py`.
fn chapter_truncate_block_parts(parts: &[String], max_chars: usize) -> String {
    let joined = parts.join(" ").trim().to_string();
    if max_chars == 0 || joined.chars().count() <= max_chars {
        return joined;
    }

    let sep = " ... ";
    let sep_len = sep.chars().count();
    let available = max_chars.saturating_sub(sep_len);
    let head_budget = available / 2;
    let mid_budget = available - head_budget;

    let take_run = |start: usize, budget: usize| -> Vec<String> {
        let mut taken: Vec<String> = Vec::new();
        let mut used: usize = 0;
        for part in &parts[start..] {
            let part_len = part.chars().count();
            let cost = part_len + usize::from(!taken.is_empty());
            if !taken.is_empty() && used + cost > budget {
                break;
            }
            taken.push(part.clone());
            used += cost;
        }
        // Always keep at least the first part; char-clip it if it alone
        // overflows the budget.
        if taken.len() == 1 && used > budget {
            taken[0] = taken[0].chars().take(budget).collect();
        }
        taken
    };

    let head_parts = take_run(0, head_budget);
    let mid_anchor = head_parts.len().max(parts.len() / 2);
    let mid_parts = if mid_anchor < parts.len() {
        take_run(mid_anchor, mid_budget)
    } else {
        Vec::new()
    };

    let head = head_parts.join(" ");
    let middle = mid_parts.join(" ");
    if middle.is_empty() {
        return head.chars().take(max_chars).collect();
    }
    format!("{head}{sep}{middle}")
}

/// Fit `text` into `max_chars` by keeping the block's opening AND a window
/// around the block's midpoint, joined by " ... ". Mirrors
/// `_truncate_block_text` in `src/podcast_processor/chapter_fallback.py`.
fn chapter_truncate_block_text(text: &str, max_chars: usize) -> String {
    let total_chars = text.chars().count();
    if max_chars == 0 || total_chars <= max_chars {
        return text.to_string();
    }
    let sep = " ... ";
    let sep_len = sep.chars().count();
    if max_chars <= sep_len + 4 {
        return text.chars().take(max_chars).collect();
    }
    let available = max_chars - sep_len;
    let head_chars = available / 2;
    let mid_chars = available - head_chars;

    let chars: Vec<char> = text.chars().collect();
    let head: String = chars[..head_chars].iter().collect();
    let midpoint = total_chars / 2;
    let mut mid_start = head_chars.max(midpoint.saturating_sub(mid_chars / 2));
    let mid_end = total_chars.min(mid_start + mid_chars);
    // Re-anchor backward so the middle window fills mid_chars when possible.
    mid_start = head_chars.max(mid_end.saturating_sub(mid_chars));
    let middle: String = chars[mid_start..mid_end].iter().collect();
    format!("{head}{sep}{middle}")
}

/// Decode the JSON list of removed audio windows produced by Python's
/// `_filter_transcript_segments_for_chapters` caller. Accepted shapes:
///   `[[start_ms, end_ms], ...]`  (preferred — matches the existing
///   `_windows_json_file` writer used by the audio sidecar paths)
///   `[{"start_ms": N, "end_ms": M}, ...]`
fn read_chapter_removed_windows(path: &Path) -> Result<Vec<(i64, i64)>> {
    let raw = std::fs::read_to_string(path)?;
    let parsed: Value = serde_json::from_str(&raw)?;
    let arr = match parsed.as_array() {
        Some(arr) => arr,
        None => return Ok(Vec::new()),
    };
    let mut out: Vec<(i64, i64)> = Vec::with_capacity(arr.len());
    for entry in arr {
        if let Some(pair) = entry.as_array() {
            if pair.len() >= 2 {
                let start = pair[0]
                    .as_i64()
                    .or_else(|| pair[0].as_f64().map(|v| v as i64));
                let end = pair[1]
                    .as_i64()
                    .or_else(|| pair[1].as_f64().map(|v| v as i64));
                if let (Some(s), Some(e)) = (start, end) {
                    out.push((s, e));
                }
            }
        } else if let Some(obj) = entry.as_object() {
            let start = obj.get("start_ms").and_then(|v| v.as_i64());
            let end = obj.get("end_ms").and_then(|v| v.as_i64());
            if let (Some(s), Some(e)) = (start, end) {
                out.push((s, e));
            }
        }
    }
    Ok(out)
}

fn filter_segments_by_removed_windows(
    segments: &[StatsTranscriptSegmentRow],
    removed_windows: &[(i64, i64)],
) -> Vec<StatsTranscriptSegmentRow> {
    let mut sorted_windows = removed_windows.to_vec();
    sorted_windows.sort_by_key(|w| w.0);
    let mut kept: Vec<StatsTranscriptSegmentRow> = Vec::with_capacity(segments.len());
    for seg in segments {
        let seg_start_ms = chapter_seg_start_ms(seg.start_time);
        let seg_end_ms = chapter_seg_end_ms(seg.end_time).max(seg_start_ms);
        if segment_overlaps_removed_windows(seg_start_ms, seg_end_ms, &sorted_windows) {
            continue;
        }
        kept.push(seg.clone());
    }
    kept
}

/// Mirror of `_segment_overlaps_removed_audio`: walks windows in sorted order
/// and returns true on the first window that overlaps the segment.
fn segment_overlaps_removed_windows(
    seg_start_ms: i64,
    seg_end_ms: i64,
    sorted_windows: &[(i64, i64)],
) -> bool {
    for (removed_start, removed_end) in sorted_windows {
        if *removed_end <= seg_start_ms {
            continue;
        }
        if *removed_start >= seg_end_ms {
            return false;
        }
        return true;
    }
    false
}

fn run_chapters_topic_blocks(args: ChaptersTopicBlocksArgs) -> Result<Value> {
    let conn = open_readonly_sqlite(&args.db)?;
    let post = query_stats_post(&conn, &args.post_guid)?
        .ok_or_else(|| anyhow!("post not found for guid {}", args.post_guid))?;
    let mut segments = query_stats_transcript_segments(&conn, post.id)?;
    // Python sorts by start_time before block-building; sequence_num order is
    // usually identical but not guaranteed (e.g. after a re-transcription).
    segments.sort_by(|a, b| {
        a.start_time
            .partial_cmp(&b.start_time)
            .unwrap_or(std::cmp::Ordering::Equal)
    });

    if let Some(path) = args.removed_windows_json.as_ref() {
        let removed_windows = read_chapter_removed_windows(path)
            .with_context(|| format!("failed to read removed windows {}", path.display()))?;
        if !removed_windows.is_empty() {
            let filtered = filter_segments_by_removed_windows(&segments, &removed_windows);
            // Parity with Python: if the filter would empty the set, keep the
            // original unfiltered segments.
            if !filtered.is_empty() {
                segments = filtered;
            }
        }
    }

    let total_duration_ms = args
        .total_duration_ms
        .or_else(|| chapter_transcript_duration_ms(&segments));
    let Some(total_duration_ms) = total_duration_ms else {
        return Ok(json!({"blocks": []}));
    };
    if total_duration_ms <= 0 {
        return Ok(json!({"blocks": []}));
    }
    let blocks = chapter_build_topic_blocks(
        &segments,
        total_duration_ms,
        args.target_block_count,
        args.min_block_seconds,
        args.max_block_seconds,
        args.max_chars_per_block,
        args.include_segment_markers,
    );
    Ok(json!({"blocks": blocks}))
}

// === Chapter fallback: topic-plan response parser. Mirrors
// `_parse_topic_chapter_response` and its salvage helpers in
// `src/podcast_processor/chapter_fallback.py`. All operations are on small
// (<10 KB) LLM responses; the win here is keeping that small allocation off
// the Python heap.

fn topic_plan_extract_count_from_text(content: &str) -> Option<i64> {
    let pattern = regex::Regex::new(r#""chapter_count"\s*:\s*(-?\d+)"#).ok()?;
    let captures = pattern.captures(content)?;
    let raw = captures.get(1)?.as_str();
    let parsed: i64 = raw.parse().ok()?;
    if parsed < 0 {
        None
    } else {
        Some(parsed)
    }
}

fn topic_plan_coerce_count(value: &Value) -> Option<i64> {
    let parsed = if let Some(n) = value.as_i64() {
        n
    } else if let Some(s) = value.as_str() {
        s.parse::<i64>().ok()?
    } else {
        let f = value.as_f64()?;
        // Match Python's int(value) coercion for floats: truncate toward zero.
        f as i64
    };
    if parsed < 0 {
        None
    } else {
        Some(parsed)
    }
}

fn topic_plan_strip_code_fences(text: &str) -> String {
    let leading = regex::Regex::new(r"(?i)^```(?:json)?\s*").unwrap();
    let trailing = regex::Regex::new(r"\s*```$").unwrap();
    let trimmed = text.trim();
    let after_leading = leading.replace(trimmed, "");
    let after_trailing = trailing.replace(&after_leading, "");
    after_trailing.to_string()
}

fn topic_plan_salvage(content: &str) -> Vec<(i64, String)> {
    let text = content.trim();
    if text.is_empty() {
        return Vec::new();
    }
    // Same regex as Python `_salvage_topic_chapter_response` — match a
    // `block_index` + `title` pair, tolerating JSON-style backslash escapes
    // inside the title.
    let pattern =
        regex::Regex::new(r#""block_index"\s*:\s*(-?\d+)\s*,\s*"title"\s*:\s*"((?:[^"\\]|\\.)*)""#)
            .expect("static regex compiles");

    let mut out: Vec<(i64, String)> = Vec::new();
    for caps in pattern.captures_iter(text) {
        let Some(idx_match) = caps.get(1) else {
            continue;
        };
        let Some(title_match) = caps.get(2) else {
            continue;
        };
        let Ok(block_index) = idx_match.as_str().parse::<i64>() else {
            continue;
        };
        let raw_title = title_match.as_str();
        // Run the title through JSON string decoding so escape sequences
        // (\n, \", \uXXXX, etc.) match Python's `json.loads(f'"{raw}"')`.
        let title = match serde_json::from_str::<String>(&format!("\"{raw_title}\"")) {
            Ok(decoded) => decoded,
            Err(_) => raw_title.to_string(),
        };
        let title = title.trim().to_string();
        if title.is_empty() {
            continue;
        }
        out.push((block_index, title));
    }

    let mut deduped: Vec<(i64, String)> = Vec::new();
    let mut seen: std::collections::HashSet<i64> = std::collections::HashSet::new();
    for (idx, title) in out {
        if !seen.insert(idx) {
            continue;
        }
        deduped.push((idx, title));
    }
    deduped
}

#[derive(Clone)]
struct TopicPlanParseResult {
    entries: Vec<(i64, String)>,
    expected_count: Option<i64>,
    /// True when the response only parsed after salvage, signalling a
    /// truncation/repair path the Python wrapper logs at WARN.
    salvaged: bool,
    /// True when `chapter_count` from the parsed JSON disagreed with the
    /// number of `chapters[]` entries the model returned.
    count_mismatch: bool,
}

fn parse_topic_chapter_plan(content: &str) -> TopicPlanParseResult {
    let text = content.trim();
    if text.is_empty() {
        return TopicPlanParseResult {
            entries: Vec::new(),
            expected_count: None,
            salvaged: false,
            count_mismatch: false,
        };
    }

    let unfenced = topic_plan_strip_code_fences(text);
    let mut expected_count = topic_plan_extract_count_from_text(&unfenced);

    let parsed: serde_json::Result<Value> = serde_json::from_str(&unfenced);
    let data = match parsed {
        Ok(v) => v,
        Err(_) => {
            let salvaged = topic_plan_salvage(&unfenced);
            return TopicPlanParseResult {
                entries: salvaged,
                expected_count,
                salvaged: true,
                count_mismatch: false,
            };
        }
    };

    let items = match data
        .as_object()
        .and_then(|obj| obj.get("chapters"))
        .and_then(|v| v.as_array())
    {
        Some(items) => items,
        None => {
            return TopicPlanParseResult {
                entries: Vec::new(),
                expected_count,
                salvaged: false,
                count_mismatch: false,
            };
        }
    };

    // Prefer the top-level `chapter_count` once we have parsed JSON; the
    // regex-from-text version was only a fallback for un-parseable payloads.
    if let Some(count_value) = data.as_object().and_then(|obj| obj.get("chapter_count")) {
        let coerced = topic_plan_coerce_count(count_value);
        if coerced.is_some() {
            expected_count = coerced;
        } else {
            expected_count = None;
        }
    }

    let mut entries: Vec<(i64, String)> = Vec::with_capacity(items.len());
    for item in items {
        let obj = match item.as_object() {
            Some(o) => o,
            None => continue,
        };
        let block_index_value = match obj.get("block_index") {
            Some(v) => v,
            None => continue,
        };
        let block_index = if let Some(n) = block_index_value.as_i64() {
            n
        } else if let Some(s) = block_index_value.as_str() {
            match s.parse::<i64>() {
                Ok(n) => n,
                Err(_) => continue,
            }
        } else if let Some(f) = block_index_value.as_f64() {
            f as i64
        } else {
            continue;
        };
        let title = obj
            .get("title")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .trim()
            .to_string();
        entries.push((block_index, title));
    }

    let count_mismatch =
        expected_count.is_some() && expected_count.unwrap() != (entries.len() as i64);

    TopicPlanParseResult {
        entries,
        expected_count,
        salvaged: false,
        count_mismatch,
    }
}

fn run_chapters_topic_plan_parse(args: ChaptersTopicPlanParseArgs) -> Result<Value> {
    let content = std::fs::read_to_string(&args.input)
        .with_context(|| format!("failed to read topic-plan input {}", args.input.display()))?;
    let result = parse_topic_chapter_plan(&content);
    let entries: Vec<Value> = result
        .entries
        .iter()
        .map(|(idx, title)| json!([idx, title]))
        .collect();
    Ok(json!({
        "entries": entries,
        "expected_count": result.expected_count,
        "salvaged": result.salvaged,
        "count_mismatch": result.count_mismatch,
    }))
}

// === Chapter fallback: topic-plan-apply. Mirrors
// `_chapters_from_topic_plan` + `_normalize_topic_plan_entries` +
// `_filter_topic_plan_entries` + `_build_chapters_from_filtered_topic_plan`
// + `_chapter_title_from_text` in `chapter_fallback.py`.

#[derive(Clone)]
struct TopicPlanApplyBlock {
    block_index: i64,
    start_ms: i64,
    text: String,
}

fn chapter_title_from_text(text: &str, index: i64) -> String {
    let normalized = chapter_normalize_whitespace(text);
    // Strip leading non-word characters; matches Python `re.sub(r"^[^\w]+", "", ...)`.
    let leading = regex::Regex::new(r"^[^\w]+").unwrap();
    let normalized = leading.replace(&normalized, "").to_string();
    if normalized.is_empty() {
        return format!("Chapter {index}");
    }
    let words: Vec<&str> = normalized.split(' ').collect();
    let take = words.len().min(8);
    let joined = words[..take].join(" ");
    let title = joined
        .trim_matches(|c: char| " -:,.".contains(c))
        .to_string();
    if title.is_empty() {
        return format!("Chapter {index}");
    }
    if words.len() > 8 {
        format!("{title}...")
    } else {
        title
    }
}

fn normalize_topic_plan_entries(
    plan: &[(i64, String)],
    block_map: &std::collections::HashMap<i64, TopicPlanApplyBlock>,
) -> Vec<(i64, String)> {
    let mut sorted = plan.to_vec();
    sorted.sort_by_key(|entry| entry.0);

    let mut normalized: Vec<(i64, String)> = Vec::new();
    let mut seen: std::collections::HashSet<i64> = std::collections::HashSet::new();
    for (block_index, title) in sorted {
        if !block_map.contains_key(&block_index) {
            continue;
        }
        if !seen.insert(block_index) {
            continue;
        }
        normalized.push((block_index, title));
    }

    if normalized.is_empty() {
        return normalized;
    }

    if normalized[0].0 != 0 {
        if let Some(block_zero) = block_map.get(&0) {
            let first_text = &block_zero.text;
            normalized.insert(0, (0, chapter_title_from_text(first_text, 1)));
        }
    }
    normalized
}

fn filter_topic_plan_entries(
    normalized: &[(i64, String)],
    block_map: &std::collections::HashMap<i64, TopicPlanApplyBlock>,
    min_chapter_gap_ms: i64,
) -> Vec<(i64, String)> {
    let mut filtered: Vec<(i64, String)> = Vec::new();
    let mut last_start_ms: Option<i64> = None;
    let last_candidate_block_index = match normalized.last() {
        Some(entry) => entry.0,
        None => return filtered,
    };

    for (block_index, title) in normalized {
        let block = match block_map.get(block_index) {
            Some(b) => b,
            None => continue,
        };
        let start_ms = block.start_ms;
        if !filtered.is_empty() {
            if let Some(last) = last_start_ms {
                let gap_ms = start_ms - last;
                let is_last_candidate = *block_index == last_candidate_block_index;
                if gap_ms < min_chapter_gap_ms && !is_last_candidate {
                    continue;
                }
            }
        }
        filtered.push((*block_index, title.clone()));
        last_start_ms = Some(start_ms);
    }
    filtered
}

fn build_chapters_from_filtered_topic_plan(
    filtered: &[(i64, String)],
    block_map: &std::collections::HashMap<i64, TopicPlanApplyBlock>,
    total_duration_ms: i64,
) -> Vec<Value> {
    let mut chapters: Vec<Value> = Vec::with_capacity(filtered.len());
    for (idx, (block_index, title)) in filtered.iter().enumerate() {
        let block = match block_map.get(block_index) {
            Some(b) => b,
            None => continue,
        };
        let start_ms = block.start_ms;
        let next_start_ms = if idx + 1 < filtered.len() {
            block_map
                .get(&filtered[idx + 1].0)
                .map(|b| b.start_ms)
                .unwrap_or(total_duration_ms)
        } else {
            total_duration_ms
        };
        let trimmed = title.trim().to_string();
        let title_value = if !trimmed.is_empty() {
            trimmed
        } else {
            chapter_title_from_text(&block.text, (idx + 1) as i64)
        };
        chapters.push(json!({
            "element_id": format!("tgen{idx}"),
            "title": title_value,
            "start_time_ms": start_ms.max(0),
            "end_time_ms": start_ms.max(next_start_ms),
        }));
    }
    chapters
}

fn chapters_from_topic_plan(
    plan: &[(i64, String)],
    blocks: &[TopicPlanApplyBlock],
    total_duration_ms: i64,
    min_chapter_gap_ms: i64,
) -> Vec<Value> {
    if plan.is_empty() || blocks.is_empty() {
        return Vec::new();
    }
    let mut block_map: std::collections::HashMap<i64, TopicPlanApplyBlock> =
        std::collections::HashMap::with_capacity(blocks.len());
    for block in blocks {
        block_map.insert(block.block_index, block.clone());
    }

    let normalized = normalize_topic_plan_entries(plan, &block_map);
    if normalized.is_empty() {
        return Vec::new();
    }
    let filtered = filter_topic_plan_entries(&normalized, &block_map, min_chapter_gap_ms);
    if filtered.is_empty() {
        return Vec::new();
    }
    // Mirror Python: a single chapter for a long episode is treated as a
    // failure so the caller can fall back to the heuristic split.
    if filtered.len() == 1 && total_duration_ms >= 20 * 60 * 1000 {
        return Vec::new();
    }
    build_chapters_from_filtered_topic_plan(&filtered, &block_map, total_duration_ms)
}

fn run_chapters_topic_plan_apply(args: ChaptersTopicPlanApplyArgs) -> Result<Value> {
    let raw = std::fs::read_to_string(&args.input).with_context(|| {
        format!(
            "failed to read topic-plan-apply input {}",
            args.input.display()
        )
    })?;
    let payload: Value =
        serde_json::from_str(&raw).with_context(|| "topic-plan-apply input was not valid JSON")?;

    let plan: Vec<(i64, String)> = payload
        .get("plan")
        .and_then(|v| v.as_array())
        .map(|arr| {
            arr.iter()
                .filter_map(|entry| {
                    let pair = entry.as_array()?;
                    if pair.len() < 2 {
                        return None;
                    }
                    let idx = pair[0].as_i64()?;
                    let title = pair[1].as_str().unwrap_or("").to_string();
                    Some((idx, title))
                })
                .collect()
        })
        .unwrap_or_default();

    let blocks: Vec<TopicPlanApplyBlock> = payload
        .get("blocks")
        .and_then(|v| v.as_array())
        .map(|arr| {
            arr.iter()
                .filter_map(|entry| {
                    let obj = entry.as_object()?;
                    Some(TopicPlanApplyBlock {
                        block_index: obj.get("block_index")?.as_i64()?,
                        start_ms: obj.get("start_ms")?.as_i64()?,
                        text: obj
                            .get("text")
                            .and_then(|v| v.as_str())
                            .unwrap_or("")
                            .to_string(),
                    })
                })
                .collect()
        })
        .unwrap_or_default();

    let total_duration_ms = payload
        .get("total_duration_ms")
        .and_then(|v| v.as_i64())
        .unwrap_or(0);
    let min_chapter_gap_ms = payload
        .get("min_chapter_gap_ms")
        .and_then(|v| v.as_i64())
        .unwrap_or(0);

    let chapters = chapters_from_topic_plan(&plan, &blocks, total_duration_ms, min_chapter_gap_ms);
    Ok(json!({"chapters": chapters}))
}

fn run_transcript_ad_merge(args: TranscriptAdMergeArgs) -> Result<Value> {
    let conn = open_readonly_sqlite(&args.db)?;
    let post = query_stats_post(&conn, &args.post_guid)?
        .ok_or_else(|| anyhow!("post not found for guid {}", args.post_guid))?;
    let transcript_segments = query_stats_transcript_segments(&conn, post.id)?;
    let audio_segments = query_stats_audio_segments(&conn, post.id)?;
    let identifications = query_stats_identifications(&conn, post.id)?;
    let ad_blocks = build_stats_ad_blocks(
        &post,
        &transcript_segments,
        &audio_segments,
        &identifications,
        args.min_confidence,
        args.max_gap,
        args.enable_boundary_refinement,
    );
    let ad_segments: Vec<Value> = ad_blocks
        .iter()
        .map(|(start, end)| json!([start, end]))
        .collect();
    Ok(json!({ "ad_segments": ad_segments }))
}

fn render_stats(args: StatsRenderArgs) -> Result<Value> {
    let conn = open_readonly_sqlite(&args.db)?;
    let post = query_stats_post(&conn, &args.post_guid)?
        .ok_or_else(|| anyhow!("post not found for guid {}", args.post_guid))?;
    let feed = query_stats_feed(&conn, post.feed_id)?;
    let model_calls = query_stats_model_calls(&conn, post.id)?;
    let recent_jobs = query_stats_processing_jobs(&conn, &post.guid)?;
    let transcript_segments = query_stats_transcript_segments(&conn, post.id)?;
    let audio_segments = query_stats_audio_segments(&conn, post.id)?;
    let identifications = query_stats_identifications(&conn, post.id)?;

    let ad_detection_strategy = feed
        .as_ref()
        .map(|feed| feed.ad_detection_strategy.as_str())
        .unwrap_or("llm");
    let (model_call_statuses, model_types) = count_stats_model_calls(&model_calls);
    let identifications_by_segment = group_stats_identifications_by_segment(&identifications);
    let (content_segments, ad_segments_count) =
        count_stats_primary_labels(&transcript_segments, &identifications_by_segment);
    let refined_windows = parse_refined_windows_json(post.refined_ad_boundaries.as_deref());
    let bleep_windows =
        parse_time_windows_json(post.bleep_windows.as_deref(), "start_time", "end_time");

    let mut segment_mixed_by_id: HashMap<i64, bool> = HashMap::new();
    let transcript_segments_data: Vec<Value> = transcript_segments
        .iter()
        .map(|segment| {
            let segment_identifications = identifications_by_segment
                .get(&segment.id)
                .cloned()
                .unwrap_or_default();
            let has_ad_label = segment_identifications
                .iter()
                .any(|ident| ident.label == "ad");
            let mixed = has_ad_label
                && is_mixed_segment(segment.start_time, segment.end_time, &refined_windows);
            segment_mixed_by_id.insert(segment.id, mixed);
            json!({
                "id": segment.id,
                "sequence_num": segment.sequence_num,
                "start_time": round_to(segment.start_time, 1),
                "end_time": round_to(segment.end_time, 1),
                "speaker_label": segment.speaker_label,
                "text": segment.text,
                "primary_label": if has_ad_label { "ad" } else { "content" },
                "mixed": mixed,
                "identifications": segment_identifications.iter().map(|ident| json!({
                    "id": ident.id,
                    "label": ident.label,
                    "confidence": ident.confidence.map(|value| round_to(value, 2)),
                    "model_call_id": ident.model_call_id,
                })).collect::<Vec<_>>(),
            })
        })
        .collect();

    let ad_blocks = build_stats_ad_blocks(
        &post,
        &transcript_segments,
        &audio_segments,
        &identifications,
        args.min_confidence,
        args.min_ad_segment_separation_seconds,
        args.enable_boundary_refinement,
    );
    let ad_time_seconds: f64 = ad_blocks
        .iter()
        .filter(|(start, end)| end > start)
        .map(|(start, end)| end - start)
        .sum();
    let original_duration_seconds = resolve_original_duration_seconds(
        post.duration,
        &transcript_segments,
        &bleep_windows,
        ad_time_seconds,
    );
    let ad_percentage = if original_duration_seconds > 0.0 {
        ad_time_seconds / original_duration_seconds * 100.0
    } else {
        0.0
    };
    let bleep_time_seconds: f64 = bleep_windows
        .iter()
        .filter(|(start, end)| end > start)
        .map(|(start, end)| end - start)
        .sum();
    let bleep_percentage = if original_duration_seconds > 0.0 {
        bleep_time_seconds / original_duration_seconds * 100.0
    } else {
        0.0
    };
    let edited_duration_seconds = (original_duration_seconds - ad_time_seconds).max(0.0);

    let chapters = build_stats_chapters(&post, feed.as_ref(), ad_detection_strategy)?;
    let related_logs = build_related_logs_for_stats(&args.log_path, &post, &recent_jobs);

    let mut stats = json!({
        "post": {
            "guid": post.guid,
            "title": post.title,
            "duration": post.duration,
            "release_date": post.release_date,
            "whitelisted": post.whitelisted,
            "has_processed_audio": post.processed_audio_path.is_some(),
            "download_count": post.download_count,
        },
        "ad_detection_strategy": ad_detection_strategy,
        "processing_stats": {
            "total_segments": transcript_segments.len(),
            "total_model_calls": model_calls.len(),
            "total_identifications": identifications.len(),
            "audio_segments_count": audio_segments.len(),
            "content_segments": content_segments,
            "ad_segments_count": ad_segments_count,
            "ad_percentage": round_to(ad_percentage, 1),
            "estimated_ad_time_seconds": round_to(ad_time_seconds, 1),
            "original_duration_seconds": round_to(original_duration_seconds, 1),
            "edited_duration_seconds": round_to(edited_duration_seconds, 1),
            "ad_blocks": ad_blocks.iter().map(|(start, end)| json!({
                "start_time": round_to(*start, 1),
                "end_time": round_to(*end, 1),
            })).collect::<Vec<_>>(),
            "edited_ad_markers": build_edited_timeline_ad_markers(&ad_blocks),
            "has_bleep_windows": !bleep_windows.is_empty(),
            "bleeped_time_seconds": round_to(bleep_time_seconds, 1),
            "bleeped_percentage": round_to(bleep_percentage, 1),
            "bleep_windows": bleep_windows.iter().map(|(start, end)| json!({
                "start_time": round_to(*start, 3),
                "end_time": round_to(*end, 3),
            })).collect::<Vec<_>>(),
            "edited_bleep_windows": build_edited_timeline_bleep_windows(&bleep_windows, &ad_blocks),
            "speaker_breakdown": build_stats_speaker_breakdown(&transcript_segments),
            "model_call_statuses": model_call_statuses,
            "model_types": model_types,
        },
        "model_calls": model_calls.iter().map(|call| {
            let recorded_attempts = call.retry_attempts.max(0);
            json!({
                "id": call.id,
                "model_name": call.model_name,
                "status": call.status,
                "segment_range": format_segment_range_label(
                    call.first_segment_sequence_num,
                    call.last_segment_sequence_num,
                ),
                "first_segment_sequence_num": call.first_segment_sequence_num,
                "last_segment_sequence_num": call.last_segment_sequence_num,
                "timestamp": call.timestamp,
                "retry_attempts": recorded_attempts,
                "retry_count": (recorded_attempts - 1).max(0),
                "error_message": call.error_message,
                "prompt": call.prompt,
                "response": call.response,
                "service_tier": call.service_tier,
                "prompt_tokens": call.prompt_tokens,
                "cached_prompt_tokens": call.cached_prompt_tokens,
                "completion_tokens": call.completion_tokens,
                "total_tokens": call.total_tokens,
            })
        }).collect::<Vec<_>>(),
        "transcript_segments": transcript_segments_data,
        "audio_segments": audio_segments.iter().map(|segment| json!({
            "id": segment.id,
            "start_time": round_to(segment.start_time, 1),
            "end_time": round_to(segment.end_time, 1),
            "label": segment.label,
            "model_call_id": segment.model_call_id,
        })).collect::<Vec<_>>(),
        "identifications": identifications.iter().map(|ident| json!({
            "id": ident.id,
            "transcript_segment_id": ident.transcript_segment_id,
            "label": ident.label,
            "confidence": ident.confidence.map(|value| round_to(value, 2)),
            "model_call_id": ident.model_call_id,
            "segment_sequence_num": ident.segment_sequence_num,
            "segment_start_time": round_to(ident.segment_start_time, 1),
            "segment_end_time": round_to(ident.segment_end_time, 1),
            "segment_text": ident.segment_text,
            "mixed": *segment_mixed_by_id.get(&ident.transcript_segment_id).unwrap_or(&false),
        })).collect::<Vec<_>>(),
        "related_logs": related_logs,
        "chapters": chapters,
    });

    if args.stats_debug {
        stats["debug_info"] = json!({
            "post_id": post.id,
            "feed_id": post.feed_id,
            "guid": post.guid,
            "download_url": post.download_url,
            "download_count": post.download_count,
            "has_processed_audio": post.processed_audio_path.is_some(),
            "has_unprocessed_audio": post.unprocessed_audio_path.is_some(),
            "processed_audio": build_file_debug(post.processed_audio_path.as_deref()),
            "unprocessed_audio": build_file_debug(post.unprocessed_audio_path.as_deref()),
            "processed_audio_path_candidates": [],
            "processing_roots": {
                "in_root": args.in_root.to_string_lossy().to_string(),
                "srv_root": args.srv_root.to_string_lossy().to_string(),
            },
            "record_counts": {
                "transcript_segments": transcript_segments.len(),
                "audio_segments": audio_segments.len(),
                "model_calls": model_calls.len(),
                "identifications": identifications.len(),
            },
        });
    }

    Ok(json!({ "stats": stats }))
}

fn render_jobs(args: JobsListArgs, active_only: bool) -> Result<Value> {
    let conn = open_readonly_sqlite(&args.db)?;
    let limit = args.limit.clamp(1, 1000);
    let status_filter = if active_only {
        "WHERE processing_job.status IN ('pending', 'running')"
    } else {
        ""
    };
    let sql = format!(
        "SELECT
            processing_job.id,
            processing_job.post_guid,
            post.title,
            feed.title,
            processing_job.status,
            CASE
                WHEN processing_job.status = 'running' THEN 2
                WHEN processing_job.status = 'pending' THEN 1
                ELSE 0
            END AS priority,
            processing_job.current_step,
            processing_job.step_name,
            processing_job.total_steps,
            processing_job.progress_percentage,
            processing_job.created_at,
            processing_job.started_at,
            processing_job.completed_at,
            processing_job.error_message,
            processing_job.stage_history,
            processing_job.ad_windows_count,
            processing_job.had_classification_parse_error,
            processing_job.auto_retry_attempted,
            post.id
         FROM processing_job
         LEFT JOIN post ON processing_job.post_guid = post.guid
         LEFT JOIN feed ON post.feed_id = feed.id
         {status_filter}
         ORDER BY priority DESC, processing_job.created_at DESC
         LIMIT ?1"
    );
    let mut stmt = conn.prepare(&sql)?;
    let rows = stmt.query_map([limit], |row| {
        // SQLAlchemy stores the JSON column as TEXT; parse it back here so
        // the API surface matches the Python `list_active_jobs` serializer.
        // Tolerate legacy rows (NULL or invalid JSON) by emitting an empty
        // array rather than failing the entire job listing.
        let stage_history_raw: Option<String> = row.get(14)?;
        let stage_history: Value = stage_history_raw
            .and_then(|raw| serde_json::from_str::<Value>(&raw).ok())
            .filter(|value| value.is_array())
            .unwrap_or_else(|| Value::Array(Vec::new()));
        let post_id: Option<i64> = row.get(18)?;
        let entry = json!({
            "job_id": row.get::<_, String>(0)?,
            "post_guid": row.get::<_, String>(1)?,
            "post_title": row.get::<_, Option<String>>(2)?,
            "feed_title": row.get::<_, Option<String>>(3)?,
            "status": row.get::<_, String>(4)?,
            "priority": row.get::<_, i64>(5)?,
            "step": row.get::<_, Option<i64>>(6)?,
            "step_name": row.get::<_, Option<String>>(7)?,
            "total_steps": row.get::<_, Option<i64>>(8)?,
            "progress_percentage": row.get::<_, Option<f64>>(9)?,
            "created_at": row.get::<_, Option<String>>(10)?.map(|value| sqlite_datetime_to_iso(&value)),
            "started_at": row.get::<_, Option<String>>(11)?.map(|value| sqlite_datetime_to_iso(&value)),
            "completed_at": row.get::<_, Option<String>>(12)?.map(|value| sqlite_datetime_to_iso(&value)),
            "error_message": row.get::<_, Option<String>>(13)?,
            "stage_history": stage_history,
            "ad_windows_count": row.get::<_, Option<i64>>(15)?,
            "had_classification_parse_error": row.get::<_, bool>(16)?,
            "auto_retry_attempted": row.get::<_, bool>(17)?,
        });
        Ok((post_id, entry))
    })?;
    let entries: Vec<(Option<i64>, Value)> = rows.collect::<std::result::Result<Vec<_>, _>>()?;

    // Bulk-lookup service_tier summary per post so the chip shows up on the
    // jobs page (same shape as the Python `_summarize_service_tiers_for_posts`
    // helper). Latest = most recent non-null tier; mixed = >1 distinct tier
    // across this post's ModelCall rows (e.g. Flex 503 → standard fallback).
    let post_ids: Vec<i64> = entries.iter().filter_map(|(id, _)| *id).collect();
    let tier_by_post = load_service_tier_summary(&conn, &post_ids)?;

    let jobs: Vec<Value> = entries
        .into_iter()
        .map(|(post_id, mut entry)| {
            if let Some(pid) = post_id {
                if let Some(tier) = tier_by_post.get(&pid) {
                    if let Some(obj) = entry.as_object_mut() {
                        obj.insert("service_tier".to_string(), tier.clone());
                    }
                }
            }
            entry
        })
        .collect();

    Ok(json!({ "jobs": jobs }))
}

// Mirror Python `_TIER_IN_FLIGHT_STATUSES`: only these statuses warrant the
// "in_flight" subobject because the wrapper retry loop hasn't terminated.
const TIER_IN_FLIGHT_STATUSES: &[&str] = &["pending", "retrying"];

struct TierBucket {
    latest: String,
    latest_status: String,
    latest_attempt: i64,
    latest_next_retry_at: Option<String>,
    latest_call_label: Option<String>,
    tiers: HashSet<String>,
}

// Short human label for what kind of LLM call is in flight. Mirrors
// `_in_flight_call_label` in app/jobs_manager.py: chapter-phase calls use
// sentinel segment ranges, and naming them keeps the jobs caption sensible
// when an LLM call runs inside a stage like "Processing audio".
fn in_flight_call_label(first_seq: i64, last_seq: i64) -> Option<String> {
    match (first_seq, last_seq) {
        (-100, -100) => Some("chapter titles".to_string()),
        (-200, -200) | (-201, -201) => Some("chapter topic plan".to_string()),
        _ => None,
    }
}

fn load_service_tier_summary(conn: &Connection, post_ids: &[i64]) -> Result<HashMap<i64, Value>> {
    if post_ids.is_empty() {
        return Ok(HashMap::new());
    }
    // Build a parameterized IN clause; rusqlite's `params_from_iter` handles
    // the binding. Ordering by timestamp DESC means the first row seen per
    // post is the most recent tier, so HashMap::entry(or_insert) records
    // `latest` correctly even though we keep walking to detect mixed.
    let placeholders: Vec<String> = (1..=post_ids.len()).map(|i| format!("?{}", i)).collect();
    let sql = format!(
        "SELECT post_id, service_tier, status, retry_attempts, next_retry_at,
                first_segment_sequence_num, last_segment_sequence_num
         FROM model_call
         WHERE service_tier IS NOT NULL AND post_id IN ({})
         ORDER BY timestamp DESC",
        placeholders.join(",")
    );
    let mut stmt = conn.prepare(&sql)?;
    let rows = stmt.query_map(rusqlite::params_from_iter(post_ids.iter()), |row| {
        Ok((
            row.get::<_, i64>(0)?,
            row.get::<_, String>(1)?,
            row.get::<_, String>(2)?,
            row.get::<_, Option<i64>>(3)?.unwrap_or(0),
            row.get::<_, Option<String>>(4)?,
            row.get::<_, i64>(5)?,
            row.get::<_, i64>(6)?,
        ))
    })?;

    let mut per_post: HashMap<i64, TierBucket> = HashMap::new();
    for r in rows {
        let (pid, tier, status, retry_attempts, next_retry_at, first_seq, last_seq) = r?;
        let entry = per_post.entry(pid).or_insert_with(|| TierBucket {
            latest: tier.clone(),
            latest_status: status,
            latest_attempt: retry_attempts,
            latest_next_retry_at: next_retry_at,
            latest_call_label: in_flight_call_label(first_seq, last_seq),
            tiers: HashSet::new(),
        });
        entry.tiers.insert(tier);
    }

    let max_retries = load_llm_max_retry_attempts(conn);

    Ok(per_post
        .into_iter()
        .map(|(pid, bucket)| (pid, build_tier_summary(&bucket, max_retries)))
        .collect())
}

fn build_tier_summary(bucket: &TierBucket, max_retries: Option<i64>) -> Value {
    let mut summary = serde_json::Map::new();
    summary.insert("label".to_string(), Value::String(bucket.latest.clone()));
    summary.insert("latest".to_string(), Value::String(bucket.latest.clone()));
    summary.insert("mixed".to_string(), Value::Bool(bucket.tiers.len() > 1));
    if TIER_IN_FLIGHT_STATUSES.contains(&bucket.latest_status.as_str()) {
        let mut in_flight = serde_json::Map::new();
        in_flight.insert(
            "status".to_string(),
            Value::String(bucket.latest_status.clone()),
        );
        // retry_attempts is 0 until the first attempt's bump lands (and some
        // call paths only set it on completion), but a pending/retrying row
        // always means an attempt is underway -- never show "attempt 0".
        in_flight.insert(
            "attempt".to_string(),
            Value::Number(bucket.latest_attempt.max(1).into()),
        );
        if let Some(max) = max_retries {
            in_flight.insert("max_retries".to_string(), Value::Number(max.into()));
        }
        if bucket.latest_status == "retrying" {
            if let Some(raw) = bucket.latest_next_retry_at.as_deref() {
                // Naive UTC text from SQLAlchemy ("YYYY-MM-DD HH:MM:SS[.ffffff]");
                // normalize to the ISO-with-Z shape Python emits.
                let mut iso = raw.replacen(' ', "T", 1);
                if !iso.ends_with('Z') {
                    iso.push('Z');
                }
                in_flight.insert("backoff_until".to_string(), Value::String(iso));
            }
        }
        if let Some(label) = bucket.latest_call_label.as_deref() {
            in_flight.insert("call_label".to_string(), Value::String(label.to_string()));
        }
        summary.insert("in_flight".to_string(), Value::Object(in_flight));
    }
    Value::Object(summary)
}

fn load_llm_max_retry_attempts(conn: &Connection) -> Option<i64> {
    // Singleton LLMSettings row; best-effort -- treat any failure (table
    // missing, row missing) as "unknown" so callers omit the max from the
    // summary rather than fail the request.
    conn.query_row(
        "SELECT llm_max_retry_attempts FROM llm_settings WHERE id = 1",
        [],
        |row| row.get::<_, Option<i64>>(0),
    )
    .ok()
    .flatten()
}

const JOBS_MANAGER_SINGLETON_RUN_ID: &str = "jobs-manager-singleton";

// Read-only equivalent of `build_run_status_snapshot` in
// app/jobs_manager_run_service.py: load the singleton run row, aggregate the
// processing_job statuses scoped to that run, and shape the response the
// same way the Python serializer does so the Flask route can transparently
// fall through to either implementation.
fn render_jobs_status(args: JobsStatusArgs) -> Result<Value> {
    let conn = open_readonly_sqlite(&args.db)?;

    let run = conn.query_row(
        "SELECT id, status, trigger, started_at, completed_at, updated_at,
                counters_reset_at, context_json
         FROM jobs_manager_run WHERE id = ?1",
        [JOBS_MANAGER_SINGLETON_RUN_ID],
        |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, Option<String>>(1)?,
                row.get::<_, Option<String>>(2)?,
                row.get::<_, Option<String>>(3)?,
                row.get::<_, Option<String>>(4)?,
                row.get::<_, Option<String>>(5)?,
                row.get::<_, Option<String>>(6)?,
                row.get::<_, Option<String>>(7)?,
            ))
        },
    );

    // `prior_status` is intentionally discarded: Python computes status from
    // the live counts, not from whatever was persisted on the run row.
    let (
        run_id,
        _prior_status,
        trigger,
        started_at,
        completed_at,
        updated_at,
        counters_reset_at,
        context_json_raw,
    ) = match run {
        Ok(values) => values,
        // The Python jobs-manager-status route returns `{"run": null}` when
        // the singleton row hasn't been created yet; mirror that shape so
        // callers can pass our payload through unchanged.
        Err(rusqlite::Error::QueryReturnedNoRows) => return Ok(json!({ "run": Value::Null })),
        Err(err) => return Err(err.into()),
    };

    let mut count_query =
        String::from("SELECT status, COUNT(id) FROM processing_job WHERE jobs_manager_run_id = ?1");
    let mut params: Vec<rusqlite::types::Value> = vec![run_id.clone().into()];
    if let Some(cutoff) = counters_reset_at.as_deref() {
        count_query.push_str(" AND created_at >= ?2");
        params.push(cutoff.to_string().into());
    }
    count_query.push_str(" GROUP BY status");

    let mut stmt = conn.prepare(&count_query)?;
    let mut counts: HashMap<String, i64> = HashMap::new();
    let rows = stmt.query_map(rusqlite::params_from_iter(params.iter()), |row| {
        Ok((row.get::<_, String>(0)?, row.get::<_, i64>(1)?))
    })?;
    for entry in rows {
        let (status, count) = entry?;
        counts.insert(status, count);
    }

    let queued =
        counts.get("pending").copied().unwrap_or(0) + counts.get("queued").copied().unwrap_or(0);
    let running = counts.get("running").copied().unwrap_or(0);
    let completed = counts.get("completed").copied().unwrap_or(0);
    let failed =
        counts.get("failed").copied().unwrap_or(0) + counts.get("cancelled").copied().unwrap_or(0);
    let skipped = counts.get("skipped").copied().unwrap_or(0);
    let total_jobs: i64 = counts.values().sum();
    let has_active_work = (queued + running) > 0;

    let status = if has_active_work {
        if running > 0 { "running" } else { "pending" }.to_string()
    } else {
        // Matches Python: idle manager reports "pending".
        "pending".to_string()
    };

    let progress_percentage = if total_jobs > 0 {
        let pct =
            (f64::from((completed + skipped) as i32) / f64::from(total_jobs.max(1) as i32)) * 100.0;
        (pct * 100.0).round() / 100.0
    } else {
        0.0
    };

    let context_value: Value = context_json_raw
        .and_then(|raw| serde_json::from_str::<Value>(&raw).ok())
        .unwrap_or(Value::Null);

    Ok(json!({
        "run": {
            "id": run_id,
            "status": status,
            "trigger": trigger,
            "started_at": started_at.map(|value| sqlite_datetime_to_iso(&value)),
            "completed_at": completed_at.map(|value| sqlite_datetime_to_iso(&value)),
            "updated_at": updated_at.map(|value| sqlite_datetime_to_iso(&value)),
            "total_jobs": total_jobs,
            "queued_jobs": queued,
            "running_jobs": running,
            "completed_jobs": completed,
            "failed_jobs": failed,
            "skipped_jobs": skipped,
            "context": context_value,
            "counters_reset_at": counters_reset_at.map(|value| sqlite_datetime_to_iso(&value)),
            "progress_percentage": progress_percentage,
        }
    }))
}

fn query_stats_post(conn: &Connection, guid: &str) -> Result<Option<StatsPostRow>> {
    let mut stmt = conn.prepare(
        "SELECT id, feed_id, guid, title, download_url, unprocessed_audio_path, processed_audio_path,
                release_date, duration, whitelisted, download_count, chapter_data,
                bleep_windows, refined_ad_boundaries
         FROM post WHERE guid = ?1 LIMIT 1",
    )?;
    let mut rows = stmt.query([guid])?;
    if let Some(row) = rows.next()? {
        Ok(Some(StatsPostRow {
            id: row.get(0)?,
            feed_id: row.get(1)?,
            guid: row.get(2)?,
            title: row.get(3)?,
            download_url: row.get(4)?,
            unprocessed_audio_path: row.get(5)?,
            processed_audio_path: row.get(6)?,
            release_date: row
                .get::<_, Option<String>>(7)?
                .map(|value| sqlite_datetime_to_iso(&value)),
            duration: get_duration_f64(row, 8)?,
            whitelisted: row.get(9)?,
            download_count: row.get(10)?,
            chapter_data: row.get(11)?,
            bleep_windows: row.get(12)?,
            refined_ad_boundaries: row.get(13)?,
        }))
    } else {
        Ok(None)
    }
}

fn query_stats_feed(conn: &Connection, feed_id: i64) -> Result<Option<StatsFeedRow>> {
    let mut stmt = conn
        .prepare("SELECT ad_detection_strategy, chapter_filter_strings FROM feed WHERE id = ?1")?;
    let mut rows = stmt.query([feed_id])?;
    if let Some(row) = rows.next()? {
        Ok(Some(StatsFeedRow {
            ad_detection_strategy: row
                .get::<_, Option<String>>(0)?
                .unwrap_or_else(|| "llm".to_string()),
            chapter_filter_strings: row.get(1)?,
        }))
    } else {
        Ok(None)
    }
}

fn query_stats_model_calls(conn: &Connection, post_id: i64) -> Result<Vec<StatsModelCallRow>> {
    let mut stmt = conn.prepare(
        "SELECT id, model_name, status, first_segment_sequence_num, last_segment_sequence_num,
                timestamp, retry_attempts, error_message, prompt, response, service_tier,
                prompt_tokens, cached_prompt_tokens, completion_tokens, total_tokens
         FROM model_call WHERE post_id = ?1
         ORDER BY model_name, first_segment_sequence_num",
    )?;
    let rows = stmt.query_map([post_id], |row| {
        Ok(StatsModelCallRow {
            id: row.get(0)?,
            model_name: row.get(1)?,
            status: row.get(2)?,
            first_segment_sequence_num: row.get(3)?,
            last_segment_sequence_num: row.get(4)?,
            timestamp: row
                .get::<_, Option<String>>(5)?
                .map(|value| sqlite_datetime_to_iso(&value)),
            retry_attempts: row.get::<_, Option<i64>>(6)?.unwrap_or(0),
            error_message: row.get(7)?,
            prompt: row.get(8)?,
            response: row.get(9)?,
            service_tier: row.get(10)?,
            prompt_tokens: row.get(11)?,
            cached_prompt_tokens: row.get(12)?,
            completion_tokens: row.get(13)?,
            total_tokens: row.get(14)?,
        })
    })?;
    Ok(rows.collect::<std::result::Result<Vec<_>, _>>()?)
}

// Sentinel (first_seq, last_seq) pairs used by chapter_fallback.py when it
// creates ModelCall rows for phases that have no real transcript segment
// range. Mirror keep in sync with `_SEGMENT_RANGE_LABELS` in post_routes.py
// and the constants in chapter_fallback.py.
fn format_segment_range_label(first_seq: i64, last_seq: i64) -> String {
    match (first_seq, last_seq) {
        (-100, -100) => "chapter titles (LLM)".to_string(),
        (-200, -200) => "chapter topic plan (LLM)".to_string(),
        (-201, -201) => "chapter topic plan: continuation (LLM)".to_string(),
        _ => format!("{first_seq}-{last_seq}"),
    }
}

fn query_stats_processing_jobs(
    conn: &Connection,
    guid: &str,
) -> Result<Vec<StatsProcessingJobRow>> {
    let mut stmt = conn.prepare(
        "SELECT id FROM processing_job WHERE post_guid = ?1 ORDER BY created_at DESC LIMIT 3",
    )?;
    let rows = stmt.query_map([guid], |row| Ok(StatsProcessingJobRow { id: row.get(0)? }))?;
    Ok(rows.collect::<std::result::Result<Vec<_>, _>>()?)
}

fn query_stats_transcript_segments(
    conn: &Connection,
    post_id: i64,
) -> Result<Vec<StatsTranscriptSegmentRow>> {
    let mut stmt = conn.prepare(
        "SELECT id, sequence_num, start_time, end_time, text, speaker_label
         FROM transcript_segment WHERE post_id = ?1 ORDER BY sequence_num",
    )?;
    let rows = stmt.query_map([post_id], |row| {
        Ok(StatsTranscriptSegmentRow {
            id: row.get(0)?,
            sequence_num: row.get(1)?,
            start_time: row.get(2)?,
            end_time: row.get(3)?,
            text: row.get(4)?,
            speaker_label: row.get(5)?,
        })
    })?;
    Ok(rows.collect::<std::result::Result<Vec<_>, _>>()?)
}

/// Range-filtered variant used by the bundled `wb-refine-from-llm` path.
/// Production always supplies `first_seq` / `last_seq` from the detected ad
/// block, so we can skip loading the thousands of out-of-window rows. The
/// ±2 padding matches `select_wb_context_segments_wb`; the extra
/// `preferred_*` slots accommodate LLM payloads whose `refined_*_segment_seq`
/// lands one or two seqs outside the window.
fn query_stats_transcript_segments_in_seq_range(
    conn: &Connection,
    post_id: i64,
    low: i64,
    high: i64,
) -> Result<Vec<StatsTranscriptSegmentRow>> {
    let mut stmt = conn.prepare(
        "SELECT id, sequence_num, start_time, end_time, text, speaker_label
         FROM transcript_segment
         WHERE post_id = ?1 AND sequence_num BETWEEN ?2 AND ?3
         ORDER BY sequence_num",
    )?;
    let rows = stmt.query_map([post_id, low, high], |row| {
        Ok(StatsTranscriptSegmentRow {
            id: row.get(0)?,
            sequence_num: row.get(1)?,
            start_time: row.get(2)?,
            end_time: row.get(3)?,
            text: row.get(4)?,
            speaker_label: row.get(5)?,
        })
    })?;
    Ok(rows.collect::<std::result::Result<Vec<_>, _>>()?)
}

fn query_stats_audio_segments(
    conn: &Connection,
    post_id: i64,
) -> Result<Vec<StatsAudioSegmentRow>> {
    let mut stmt = conn.prepare(
        "SELECT id, start_time, end_time, label, model_call_id
         FROM audio_segment WHERE post_id = ?1 ORDER BY start_time",
    )?;
    let rows = stmt.query_map([post_id], |row| {
        Ok(StatsAudioSegmentRow {
            id: row.get(0)?,
            start_time: row.get(1)?,
            end_time: row.get(2)?,
            label: row.get(3)?,
            model_call_id: row.get(4)?,
        })
    })?;
    Ok(rows.collect::<std::result::Result<Vec<_>, _>>()?)
}

fn query_stats_identifications(
    conn: &Connection,
    post_id: i64,
) -> Result<Vec<StatsIdentificationRow>> {
    let mut stmt = conn.prepare(
        "SELECT identification.id, identification.transcript_segment_id, identification.label,
                identification.confidence, identification.model_call_id, model_call.status,
                transcript_segment.sequence_num, transcript_segment.start_time,
                transcript_segment.end_time, transcript_segment.text
         FROM identification
         JOIN transcript_segment ON identification.transcript_segment_id = transcript_segment.id
         JOIN model_call ON identification.model_call_id = model_call.id
         WHERE transcript_segment.post_id = ?1
         ORDER BY transcript_segment.sequence_num",
    )?;
    let rows = stmt.query_map([post_id], |row| {
        Ok(StatsIdentificationRow {
            id: row.get(0)?,
            transcript_segment_id: row.get(1)?,
            label: row.get(2)?,
            confidence: row.get(3)?,
            model_call_id: row.get(4)?,
            model_call_status: row.get(5)?,
            segment_sequence_num: row.get(6)?,
            segment_start_time: row.get(7)?,
            segment_end_time: row.get(8)?,
            segment_text: row.get(9)?,
        })
    })?;
    Ok(rows.collect::<std::result::Result<Vec<_>, _>>()?)
}

// SQLite INTEGER columns can hold real values due to dynamic typing. Read as f64
// to avoid rusqlite type mismatch errors.
fn get_duration_f64(row: &rusqlite::Row<'_>, idx: usize) -> rusqlite::Result<Option<f64>> {
    row.get::<_, Option<f64>>(idx)
}

fn get_duration_seconds(row: &rusqlite::Row<'_>, idx: usize) -> rusqlite::Result<Option<i64>> {
    Ok(get_duration_f64(row, idx)?.map(|v| v as i64))
}

fn get_numeric_json_value(row: &rusqlite::Row<'_>, idx: usize) -> rusqlite::Result<Value> {
    Ok(match row.get_ref(idx)? {
        ValueRef::Null => Value::Null,
        ValueRef::Integer(value) => json!(value),
        ValueRef::Real(value) => serde_json::Number::from_f64(value)
            .map(Value::Number)
            .unwrap_or(Value::Null),
        ValueRef::Text(bytes) => {
            let text = String::from_utf8_lossy(bytes);
            let text = text.trim();
            if text.is_empty() {
                Value::Null
            } else if let Ok(value) = text.parse::<i64>() {
                json!(value)
            } else if let Ok(value) = text.parse::<f64>() {
                serde_json::Number::from_f64(value)
                    .map(Value::Number)
                    .unwrap_or(Value::Null)
            } else {
                Value::Null
            }
        }
        ValueRef::Blob(_) => Value::Null,
    })
}

fn round_to(value: f64, places: i32) -> f64 {
    let factor = 10_f64.powi(places);
    // Python's round() uses ties-to-even; keep sidecar JSON parity at exact
    // half-way values such as a per-subscriber cost of 0.16125.
    (value * factor).round_ties_even() / factor
}

fn sqlite_datetime_to_iso(value: &str) -> String {
    let iso = if value.contains('T') {
        value.to_string()
    } else {
        value.replace(' ', "T")
    };
    iso.strip_suffix(".000000").unwrap_or(&iso).to_string()
}

fn count_stats_model_calls(
    model_calls: &[StatsModelCallRow],
) -> (HashMap<String, i64>, HashMap<String, i64>) {
    let mut statuses = HashMap::new();
    let mut types = HashMap::new();
    for call in model_calls {
        *statuses.entry(call.status.clone()).or_insert(0) += 1;
        *types.entry(call.model_name.clone()).or_insert(0) += 1;
    }
    (statuses, types)
}

fn group_stats_identifications_by_segment(
    identifications: &[StatsIdentificationRow],
) -> HashMap<i64, Vec<StatsIdentificationRow>> {
    let mut grouped: HashMap<i64, Vec<StatsIdentificationRow>> = HashMap::new();
    for ident in identifications {
        grouped
            .entry(ident.transcript_segment_id)
            .or_default()
            .push(ident.clone());
    }
    grouped
}

fn count_stats_primary_labels(
    segments: &[StatsTranscriptSegmentRow],
    grouped: &HashMap<i64, Vec<StatsIdentificationRow>>,
) -> (usize, usize) {
    let mut content = 0;
    let mut ads = 0;
    for segment in segments {
        if grouped
            .get(&segment.id)
            .map(|items| items.iter().any(|ident| ident.label == "ad"))
            .unwrap_or(false)
        {
            ads += 1;
        } else {
            content += 1;
        }
    }
    (content, ads)
}

fn parse_refined_windows_json(raw: Option<&str>) -> Vec<(f64, f64)> {
    parse_time_windows_json(raw, "refined_start", "refined_end")
}

fn parse_time_windows_json(raw: Option<&str>, start_key: &str, end_key: &str) -> Vec<(f64, f64)> {
    let Some(raw) = raw else {
        return Vec::new();
    };
    let Ok(Value::Array(items)) = serde_json::from_str::<Value>(raw) else {
        return Vec::new();
    };
    items
        .iter()
        .filter_map(|item| {
            let object = item.as_object()?;
            let start = value_to_f64(object.get(start_key)?)?;
            let end = value_to_f64(object.get(end_key)?)?;
            (end > start).then_some((start, end))
        })
        .collect()
}

fn is_mixed_segment(seg_start: f64, seg_end: f64, refined_windows: &[(f64, f64)]) -> bool {
    refined_windows.iter().any(|(win_start, win_end)| {
        let overlaps = seg_start <= *win_end && seg_end >= *win_start;
        let fully_contained = seg_start >= *win_start && seg_end <= *win_end;
        overlaps && !fully_contained
    })
}

fn build_stats_speaker_breakdown(segments: &[StatsTranscriptSegmentRow]) -> Vec<Value> {
    let mut totals: HashMap<Option<String>, (f64, i64)> = HashMap::new();
    let mut total_time = 0.0;
    for segment in segments {
        let duration = segment.end_time - segment.start_time;
        if duration <= 0.0 {
            continue;
        }
        let label = segment.speaker_label.as_ref().and_then(|value| {
            let trimmed = value.trim();
            (!trimmed.is_empty()).then(|| trimmed.to_string())
        });
        let entry = totals.entry(label).or_insert((0.0, 0));
        entry.0 += duration;
        entry.1 += 1;
        total_time += duration;
    }
    let mut entries: Vec<_> = totals.into_iter().collect();
    entries.sort_by(|(label_a, (time_a, _)), (label_b, (time_b, _))| {
        time_b
            .partial_cmp(time_a)
            .unwrap_or(std::cmp::Ordering::Equal)
            .then_with(|| label_a.is_none().cmp(&label_b.is_none()))
            .then_with(|| label_a.cmp(label_b))
    });
    entries
        .into_iter()
        .map(|(label, (time, count))| {
            json!({
                "speaker_label": label,
                "speaking_time_seconds": round_to(time, 1),
                "speaking_percentage": round_to(if total_time > 0.0 { time / total_time * 100.0 } else { 0.0 }, 1),
                "segment_count": count,
            })
        })
        .collect()
}

fn build_stats_ad_blocks(
    post: &StatsPostRow,
    transcript_segments: &[StatsTranscriptSegmentRow],
    audio_segments: &[StatsAudioSegmentRow],
    identifications: &[StatsIdentificationRow],
    min_confidence: f64,
    max_gap: f64,
    enable_boundary_refinement: bool,
) -> Vec<(f64, f64)> {
    let segment_by_id: HashMap<i64, StatsTranscriptSegmentRow> = transcript_segments
        .iter()
        .map(|segment| (segment.id, segment.clone()))
        .collect();
    let ad_identifications: Vec<StatsIdentificationRow> = identifications
        .iter()
        .filter(|ident| ident.label == "ad")
        .filter(|ident| ident.confidence.unwrap_or(0.0) >= min_confidence)
        .filter(|ident| ident.model_call_status == "success")
        .cloned()
        .collect();
    let mut ad_segments: Vec<StatsTranscriptSegmentRow> = ad_identifications
        .iter()
        .filter_map(|ident| segment_by_id.get(&ident.transcript_segment_id).cloned())
        .collect();
    ad_segments.sort_by(|a, b| {
        a.start_time
            .partial_cmp(&b.start_time)
            .unwrap_or(std::cmp::Ordering::Equal)
    });
    if ad_segments.is_empty() {
        return Vec::new();
    }

    let groups = merge_stats_ad_segments(&ad_segments, &ad_identifications, max_gap);
    let refined_boundaries = if enable_boundary_refinement {
        parse_refined_boundaries_full(post.refined_ad_boundaries.as_deref())
    } else {
        Vec::new()
    };
    let mut ad_windows: Vec<(f64, f64)> = groups
        .iter()
        .map(|group| cut_window_for_stats_ad_group(group, &refined_boundaries))
        .collect();
    let bridgeable_audio = extract_audio_windows(audio_segments, false);
    if !bridgeable_audio.is_empty() {
        ad_windows = bridge_ad_windows_with_audio(&ad_windows, &bridgeable_audio);
    }
    let edge_audio = extract_audio_windows(audio_segments, true);
    if !edge_audio.is_empty()
        && !has_transcript_content_before_first_ad(
            transcript_segments,
            &ad_windows,
            &ad_identifications,
        )
    {
        ad_windows = expand_episode_edge_ad_windows_with_audio(&ad_windows, &edge_audio);
    }
    ad_windows.sort_by(|a, b| a.0.partial_cmp(&b.0).unwrap_or(std::cmp::Ordering::Equal));
    ad_windows
}

fn merge_stats_ad_segments(
    segments: &[StatsTranscriptSegmentRow],
    identifications: &[StatsIdentificationRow],
    max_gap: f64,
) -> Vec<StatsAdGroup> {
    let mut groups = Vec::new();
    let mut current = Vec::new();
    for segment in segments {
        if current
            .last()
            .map(|last: &StatsTranscriptSegmentRow| segment.start_time - last.end_time <= max_gap)
            .unwrap_or(true)
        {
            current.push(segment.clone());
        } else {
            groups.push(create_stats_ad_group(&current, identifications));
            current = vec![segment.clone()];
        }
    }
    if !current.is_empty() {
        groups.push(create_stats_ad_group(&current, identifications));
    }

    let refined = refine_stats_ad_groups(groups);
    refined
        .into_iter()
        .filter(is_valid_stats_ad_group)
        .collect()
}

fn create_stats_ad_group(
    segments: &[StatsTranscriptSegmentRow],
    identifications: &[StatsIdentificationRow],
) -> StatsAdGroup {
    let segment_ids: HashSet<i64> = segments.iter().map(|segment| segment.id).collect();
    let ids: Vec<_> = identifications
        .iter()
        .filter(|ident| segment_ids.contains(&ident.transcript_segment_id))
        .cloned()
        .collect();
    let confidence_avg = if ids.is_empty() {
        0.0
    } else {
        ids.iter()
            .map(|ident| ident.confidence.unwrap_or(0.0))
            .sum::<f64>()
            / ids.len() as f64
    };
    StatsAdGroup {
        segments: segments.to_vec(),
        identifications: ids,
        start_time: segments
            .first()
            .map(|segment| segment.start_time)
            .unwrap_or(0.0),
        end_time: segments
            .last()
            .map(|segment| segment.end_time)
            .unwrap_or(0.0),
        confidence_avg,
        keywords: extract_stats_keywords(segments),
    }
}

fn extract_stats_keywords(segments: &[StatsTranscriptSegmentRow]) -> Vec<String> {
    static URL_RE: OnceLock<Regex> = OnceLock::new();
    static PROMO_RE: OnceLock<Regex> = OnceLock::new();
    static PHONE_RE: OnceLock<Regex> = OnceLock::new();
    static BRAND_RE: OnceLock<Regex> = OnceLock::new();

    let text = segments
        .iter()
        .map(|segment| segment.text.as_str())
        .collect::<Vec<_>>()
        .join(" ");
    let lower = text.to_lowercase();
    let mut keywords = HashSet::new();
    let url_re =
        URL_RE.get_or_init(|| Regex::new(r"\b([a-z0-9\-\.]+\.(?:com|net|org|io))\b").unwrap());
    for capture in url_re.captures_iter(&lower) {
        keywords.insert(capture[1].to_string());
    }
    let promo_re = PROMO_RE.get_or_init(|| Regex::new(r"\b(code|promo|save)\s+\w+\b").unwrap());
    for capture in promo_re.captures_iter(&lower) {
        keywords.insert(capture[1].to_string());
    }
    let phone_re = PHONE_RE.get_or_init(|| Regex::new(r"\b\d{3}[ -]?\d{3}[ -]?\d{4}\b").unwrap());
    if phone_re.is_match(&lower) {
        keywords.insert("phone".to_string());
    }
    let mut counts: HashMap<String, usize> = HashMap::new();
    let brand_re = BRAND_RE.get_or_init(|| Regex::new(r"\b[A-Z][a-z]+\b").unwrap());
    for capture in brand_re.captures_iter(&text) {
        let word = capture[0].to_string();
        if word.len() > 3 {
            *counts.entry(word).or_insert(0) += 1;
        }
    }
    for (word, count) in counts {
        if count >= 2 {
            keywords.insert(word.to_lowercase());
        }
    }
    keywords.into_iter().collect()
}

fn refine_stats_ad_groups(groups: Vec<StatsAdGroup>) -> Vec<StatsAdGroup> {
    if groups.len() <= 1 {
        return groups;
    }
    let mut refined = Vec::new();
    let mut i = 0;
    while i < groups.len() {
        let current = &groups[i];
        if i + 1 < groups.len() {
            let next = &groups[i + 1];
            let gap = next.start_time - current.end_time;
            if gap <= 12.0 && should_merge_stats_ad_groups(current, next) {
                let mut segments = current.segments.clone();
                segments.extend(next.segments.clone());
                let mut ids = current.identifications.clone();
                ids.extend(next.identifications.clone());
                let mut keywords: HashSet<String> = current.keywords.iter().cloned().collect();
                keywords.extend(next.keywords.iter().cloned());
                refined.push(StatsAdGroup {
                    segments,
                    identifications: ids,
                    start_time: current.start_time,
                    end_time: next.end_time,
                    confidence_avg: (current.confidence_avg + next.confidence_avg) / 2.0,
                    keywords: keywords.into_iter().collect(),
                });
                i += 2;
            } else {
                refined.push(current.clone());
                i += 1;
            }
        } else {
            refined.push(current.clone());
            i += 1;
        }
    }
    refined
}

fn should_merge_stats_ad_groups(a: &StatsAdGroup, b: &StatsAdGroup) -> bool {
    if a.confidence_avg >= 0.9 && b.confidence_avg >= 0.9 {
        return true;
    }
    let a_keywords: HashSet<_> = a.keywords.iter().collect();
    if b.keywords
        .iter()
        .any(|keyword| a_keywords.contains(keyword))
    {
        return true;
    }
    let gap = b.start_time - a.end_time;
    gap <= 10.0 && a.confidence_avg >= 0.8 && b.confidence_avg >= 0.8
}

fn is_valid_stats_ad_group(group: &StatsAdGroup) -> bool {
    let duration = group.end_time - group.start_time;
    if duration > 180.0 && group.keywords.is_empty() && group.confidence_avg < 0.9 {
        return false;
    }
    if group.segments.len() < 2 || duration <= 10.0 {
        return !group.keywords.is_empty() || group.confidence_avg >= 0.9;
    }
    true
}

// Matches Python's REFINED_BOUNDARY_MATCH_TOLERANCE_SECONDS (0.75).
const REFINED_BOUNDARY_MATCH_TOLERANCE: f64 = 0.75;
// Matches Python's UNMATCHED_BLOCK_MAX_GAP_SECONDS (30.0).
const UNMATCHED_BLOCK_MAX_GAP_SECONDS: f64 = 30.0;

fn cut_window_for_stats_ad_group(
    group: &StatsAdGroup,
    refined: &[RefinedBoundaryRow],
) -> (f64, f64) {
    let blocks = atomic_ad_blocks_for_group(group);
    if blocks.is_empty() {
        return (group.start_time, group.end_time);
    }
    let projected: Vec<(f64, f64)> = blocks
        .iter()
        .map(|block| project_atomic_block(*block, refined))
        .collect();
    // Drop far unmatched blocks, matching the Python fix (f6bb450). An atomic
    // block the refiner deliberately ignored (often a classifier false
    // positive) must not drag the cut window via min()/max() and delete real
    // episode content. Blocks within UNMATCHED_BLOCK_MAX_GAP_SECONDS are kept,
    // since the refiner may have missed the true edge by a few segments.
    let mut kept: Vec<(f64, f64)> = Vec::new();
    if !refined.is_empty() {
        let refined_start = refined
            .iter()
            .map(|r| r.refined_start)
            .fold(f64::INFINITY, f64::min);
        let refined_end = refined
            .iter()
            .map(|r| r.refined_end)
            .fold(f64::NEG_INFINITY, f64::max);
        for (proj, block) in projected.iter().zip(blocks.iter()) {
            if refined_boundaries_matching_block(*block, refined) {
                kept.push(*proj);
                continue;
            }
            let gap = if block.1 < refined_start {
                refined_start - block.1
            } else if block.0 > refined_end {
                block.0 - refined_end
            } else {
                0.0
            };
            if gap <= UNMATCHED_BLOCK_MAX_GAP_SECONDS {
                kept.push(*proj);
            }
        }
    }
    let final_projected = if kept.is_empty() { projected } else { kept };
    (
        final_projected
            .iter()
            .map(|item| item.0)
            .fold(f64::INFINITY, f64::min),
        final_projected
            .iter()
            .map(|item| item.1)
            .fold(f64::NEG_INFINITY, f64::max),
    )
}

fn atomic_ad_blocks_for_group(group: &StatsAdGroup) -> Vec<(f64, f64)> {
    let mut segments = group.segments.clone();
    segments.sort_by(|a, b| {
        a.start_time
            .partial_cmp(&b.start_time)
            .unwrap_or(std::cmp::Ordering::Equal)
    });
    if segments.is_empty() {
        return Vec::new();
    }
    let mut blocks = Vec::new();
    let mut current_start = segments[0].start_time;
    let mut current_end = segments[0].end_time;
    for segment in segments.iter().skip(1) {
        if segment.start_time - current_end <= 10.0 {
            current_end = current_end.max(segment.end_time);
        } else {
            blocks.push((current_start, current_end));
            current_start = segment.start_time;
            current_end = segment.end_time;
        }
    }
    blocks.push((current_start, current_end));
    blocks
}

fn project_atomic_block(block: (f64, f64), refined: &[RefinedBoundaryRow]) -> (f64, f64) {
    // Union every refined window overlapping this block, matching the Python
    // fix (d5c1fad). Keeping only the single best-overlap match silently
    // dropped correctly refined ad windows that shared one atomic transcript
    // block, leaving ad audio uncut.
    let mut matched: Vec<&RefinedBoundaryRow> = Vec::new();
    for boundary in refined {
        let overlap = (block.1 + REFINED_BOUNDARY_MATCH_TOLERANCE)
            .min(boundary.orig_end + REFINED_BOUNDARY_MATCH_TOLERANCE)
            - (block.0 - REFINED_BOUNDARY_MATCH_TOLERANCE)
                .max(boundary.orig_start - REFINED_BOUNDARY_MATCH_TOLERANCE);
        if overlap > 0.0 {
            matched.push(boundary);
        }
    }
    if matched.is_empty() {
        return block;
    }
    let start = matched
        .iter()
        .map(|b| b.refined_start)
        .fold(f64::INFINITY, f64::min);
    let end = matched
        .iter()
        .map(|b| b.refined_end)
        .fold(f64::NEG_INFINITY, f64::max);
    (start, end)
}

fn refined_boundaries_matching_block(
    block: (f64, f64),
    refined: &[RefinedBoundaryRow],
) -> bool {
    refined.iter().any(|boundary| {
        let overlap = (block.1 + REFINED_BOUNDARY_MATCH_TOLERANCE)
            .min(boundary.orig_end + REFINED_BOUNDARY_MATCH_TOLERANCE)
            - (block.0 - REFINED_BOUNDARY_MATCH_TOLERANCE)
                .max(boundary.orig_start - REFINED_BOUNDARY_MATCH_TOLERANCE);
        overlap > 0.0
    })
}

fn parse_refined_boundaries_full(raw: Option<&str>) -> Vec<RefinedBoundaryRow> {
    let Some(raw) = raw else { return Vec::new() };
    let Ok(Value::Array(items)) = serde_json::from_str::<Value>(raw) else {
        return Vec::new();
    };
    items
        .iter()
        .filter_map(|item| {
            let object = item.as_object()?;
            let orig_start = value_to_f64(object.get("orig_start")?)?;
            let orig_end = value_to_f64(object.get("orig_end")?)?;
            let refined_start = value_to_f64(object.get("refined_start")?)?;
            let refined_end = value_to_f64(object.get("refined_end")?)?;
            (orig_end > orig_start && refined_end > refined_start).then_some(RefinedBoundaryRow {
                orig_start,
                orig_end,
                refined_start,
                refined_end,
            })
        })
        .collect()
}

fn normalize_audio_label(label: &str) -> String {
    label.trim().replace('_', "").to_lowercase()
}

fn extract_audio_windows(
    segments: &[StatsAudioSegmentRow],
    include_speech: bool,
) -> Vec<(f64, f64)> {
    let allowed: HashSet<&str> = if include_speech {
        ["music", "silence", "noenergy", "speech"]
            .into_iter()
            .collect()
    } else {
        ["music", "silence", "noenergy"].into_iter().collect()
    };
    let windows = segments
        .iter()
        .filter(|segment| allowed.contains(normalize_audio_label(&segment.label).as_str()))
        .filter(|segment| segment.end_time > segment.start_time)
        .map(|segment| (segment.start_time, segment.end_time))
        .collect();
    merge_float_windows(windows, 0.75)
}

fn bridge_ad_windows_with_audio(
    ad_windows: &[(f64, f64)],
    audio_windows: &[(f64, f64)],
) -> Vec<(f64, f64)> {
    let merged_ads = merge_float_windows(ad_windows.to_vec(), 0.0);
    if merged_ads.is_empty() || merged_ads.len() == 1 {
        return merged_ads;
    }
    let merged_audio = merge_float_windows(audio_windows.to_vec(), 0.75);
    if merged_audio.is_empty() {
        return merged_ads;
    }
    let mut bridged = vec![merged_ads[0]];
    for (next_start, next_end) in merged_ads.into_iter().skip(1) {
        let (current_start, current_end) = *bridged.last().unwrap();
        if gap_is_covered_by_audio(current_end, next_start, &merged_audio, 0.75) {
            *bridged.last_mut().unwrap() = (current_start, next_end);
        } else {
            bridged.push((next_start, next_end));
        }
    }
    bridged
}

fn expand_episode_edge_ad_windows_with_audio(
    ad_windows: &[(f64, f64)],
    edge_audio_windows: &[(f64, f64)],
) -> Vec<(f64, f64)> {
    let mut expanded = ad_windows.to_vec();
    expanded.sort_by(|a, b| a.0.partial_cmp(&b.0).unwrap_or(std::cmp::Ordering::Equal));
    if expanded.is_empty() {
        return expanded;
    }
    let merged_audio = merge_float_windows(edge_audio_windows.to_vec(), 0.75);
    if merged_audio.is_empty() {
        return expanded;
    }
    let (first_start, first_end) = expanded[0];
    if first_start <= 30.0 {
        let mut coverage_start = first_start;
        let mut changed = false;
        for (audio_start, audio_end) in merged_audio.iter().rev() {
            if *audio_end < coverage_start - 0.75 {
                break;
            }
            if *audio_start > coverage_start + 0.75 {
                continue;
            }
            coverage_start = coverage_start.min(*audio_start);
            changed = true;
        }
        if changed && coverage_start <= 30.0 {
            expanded[0] = (coverage_start.max(0.0), first_end);
        }
    }
    expanded
}

fn gap_is_covered_by_audio(
    gap_start: f64,
    gap_end: f64,
    audio_windows: &[(f64, f64)],
    tolerance: f64,
) -> bool {
    if gap_end <= gap_start {
        return true;
    }
    let mut coverage = gap_start;
    for (audio_start, audio_end) in audio_windows {
        if *audio_end < coverage - tolerance {
            continue;
        }
        if *audio_start > coverage + tolerance {
            return false;
        }
        coverage = coverage.max(*audio_end);
        if coverage >= gap_end - tolerance {
            return true;
        }
    }
    false
}

fn merge_float_windows(mut windows: Vec<(f64, f64)>, gap_seconds: f64) -> Vec<(f64, f64)> {
    windows.retain(|(start, end)| end > start);
    windows.sort_by(|a, b| a.0.partial_cmp(&b.0).unwrap_or(std::cmp::Ordering::Equal));
    let mut merged: Vec<(f64, f64)> = Vec::new();
    for (start, end) in windows {
        if let Some(last) = merged.last_mut() {
            if start <= last.1 + gap_seconds {
                last.1 = last.1.max(end);
                continue;
            }
        }
        merged.push((start, end));
    }
    merged
}

fn has_transcript_content_before_first_ad(
    transcript_segments: &[StatsTranscriptSegmentRow],
    ad_windows: &[(f64, f64)],
    ad_identifications: &[StatsIdentificationRow],
) -> bool {
    if ad_windows.is_empty() {
        return false;
    }
    let first_start = ad_windows
        .iter()
        .map(|(start, _)| *start)
        .fold(f64::INFINITY, f64::min);
    if first_start > 30.0 {
        return false;
    }
    let ad_segment_ids: HashSet<i64> = ad_identifications
        .iter()
        .map(|ident| ident.transcript_segment_id)
        .collect();
    transcript_segments
        .iter()
        .any(|segment| segment.start_time < first_start && !ad_segment_ids.contains(&segment.id))
}

fn resolve_original_duration_seconds(
    post_duration: Option<f64>,
    transcript_segments: &[StatsTranscriptSegmentRow],
    bleep_windows: &[(f64, f64)],
    ad_time_seconds: f64,
) -> f64 {
    let mut cut_duration = post_duration.unwrap_or(0.0);
    if let Some(max_bleep_end) = bleep_windows.iter().map(|(_, end)| *end).reduce(f64::max) {
        if max_bleep_end > cut_duration {
            cut_duration = max_bleep_end;
        }
    }
    let transcript_duration = transcript_segments
        .iter()
        .map(|segment| segment.end_time)
        .reduce(f64::max)
        .unwrap_or(0.0);
    if post_duration.is_some() {
        cut_duration + ad_time_seconds
    } else {
        transcript_duration.max(cut_duration)
    }
}

fn build_edited_timeline_ad_markers(ad_windows: &[(f64, f64)]) -> Vec<Value> {
    let mut sorted = ad_windows.to_vec();
    sorted.sort_by(|a, b| a.0.partial_cmp(&b.0).unwrap_or(std::cmp::Ordering::Equal));
    let mut removed_before = 0.0;
    let mut markers = Vec::new();
    for (start, end) in sorted {
        let removed = end - start;
        if removed <= 0.0 {
            continue;
        }
        let edited_time = (start - removed_before).max(0.0);
        markers.push(json!({
            "edited_start_time": round_to(edited_time, 3),
            "edited_end_time": round_to(edited_time, 3),
            "original_start_time": round_to(start, 3),
            "original_end_time": round_to(end, 3),
            "removed_duration_seconds": round_to(removed, 3),
        }));
        removed_before += removed;
    }
    markers
}

fn build_edited_timeline_bleep_windows(
    bleep_windows: &[(f64, f64)],
    removed_windows: &[(f64, f64)],
) -> Vec<Value> {
    let mut edited = Vec::new();
    let mut sorted_bleeps = bleep_windows.to_vec();
    sorted_bleeps.sort_by(|a, b| a.0.partial_cmp(&b.0).unwrap_or(std::cmp::Ordering::Equal));
    let mut sorted_removed = removed_windows.to_vec();
    sorted_removed.sort_by(|a, b| a.0.partial_cmp(&b.0).unwrap_or(std::cmp::Ordering::Equal));
    for window in sorted_bleeps {
        for (retained_start, retained_end) in subtract_removed_windows(window, &sorted_removed) {
            let edited_start = map_time_to_edited_timeline(retained_start, &sorted_removed);
            let edited_end = map_time_to_edited_timeline(retained_end, &sorted_removed);
            if edited_end > edited_start {
                edited.push(json!({
                    "edited_start_time": round_to(edited_start, 3),
                    "edited_end_time": round_to(edited_end, 3),
                    "original_start_time": round_to(retained_start, 3),
                    "original_end_time": round_to(retained_end, 3),
                }));
            }
        }
    }
    edited
}

fn map_time_to_edited_timeline(time: f64, removed_windows: &[(f64, f64)]) -> f64 {
    let mut removed_before = 0.0;
    for (start, end) in removed_windows {
        if time >= *end {
            removed_before += end - start;
        } else if time > *start {
            removed_before += time - start;
            break;
        } else {
            break;
        }
    }
    (time - removed_before).max(0.0)
}

fn subtract_removed_windows(window: (f64, f64), removed_windows: &[(f64, f64)]) -> Vec<(f64, f64)> {
    let mut remaining = vec![window];
    for (removed_start, removed_end) in removed_windows {
        let mut updated = Vec::new();
        for (segment_start, segment_end) in remaining {
            if *removed_end <= segment_start || *removed_start >= segment_end {
                updated.push((segment_start, segment_end));
            } else {
                if *removed_start > segment_start {
                    updated.push((segment_start, *removed_start));
                }
                if *removed_end < segment_end {
                    updated.push((*removed_end, segment_end));
                }
            }
        }
        remaining = updated
            .into_iter()
            .filter(|(start, end)| end > start)
            .collect();
    }
    remaining
}

fn build_stats_chapters(
    post: &StatsPostRow,
    feed: Option<&StatsFeedRow>,
    strategy: &str,
) -> Result<Value> {
    if strategy != "chapter" && strategy != "chapter_insert" {
        return Ok(Value::Null);
    }
    let Some(raw) = post.chapter_data.as_deref() else {
        if post.processed_audio_path.is_some() && feed.is_some() {
            return Err(anyhow!(
                "stats chapters require audio chapter read; falling back to Python"
            ));
        }
        return Ok(Value::Null);
    };
    let data: Value = serde_json::from_str(raw).unwrap_or(Value::Null);
    let kept = data
        .get("chapters_kept")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let removed = data
        .get("chapters_removed")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    if kept.is_empty() && removed.is_empty() {
        return Ok(Value::Null);
    }
    Ok(json!({
        "chapters_kept": kept,
        "chapters_removed": removed,
        "chapters_kept_count": kept.len(),
        "chapters_removed_count": removed.len(),
        "filter_strings": feed.and_then(|feed| feed.chapter_filter_strings.clone()),
    }))
}

fn build_related_logs_for_stats(
    log_path: &Path,
    post: &StatsPostRow,
    recent_jobs: &[StatsProcessingJobRow],
) -> Value {
    let latest_job_id = recent_jobs.first().map(|job| job.id.clone());
    let Ok(lines) = tail_log_lines(log_path, 1_000_000) else {
        return json!({ "latest_job_id": latest_job_id, "entries": [] });
    };
    if lines.is_empty() {
        return json!({ "latest_job_id": latest_job_id, "entries": [] });
    }
    let recent_job_ids: HashSet<String> = recent_jobs.iter().map(|job| job.id.clone()).collect();
    let post_id_text = post.id.to_string();
    let mut entries = Vec::new();
    static LINE_RE: OnceLock<Regex> = OnceLock::new();
    let line_re = LINE_RE.get_or_init(|| {
        Regex::new(r"^(?P<timestamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) (?P<level>[A-Z]+)\s+(?P<message>.*)$").unwrap()
    });
    for line in lines {
        let Some(caps) = line_re.captures(&line) else {
            continue;
        };
        let message = caps.name("message").map(|m| m.as_str()).unwrap_or("");
        let job_id = extract_log_field(message, "job_id");
        let post_guid = extract_log_field(message, "post_guid");
        let matches_job = job_id
            .as_ref()
            .map(|value| recent_job_ids.contains(value))
            .unwrap_or(false);
        let matches_guid = post_guid.as_deref() == Some(post.guid.as_str());
        let matches_post_id = message.contains(&format!("post_id={post_id_text}"))
            || message.contains(&format!("post {post_id_text}"));
        if !(matches_job || matches_guid || matches_post_id) {
            continue;
        }
        let step_name = extract_step_name(message);
        entries.push(json!({
            "timestamp": caps.name("timestamp").map(|m| m.as_str()).unwrap_or(""),
            "level": caps.name("level").map(|m| m.as_str()).unwrap_or(""),
            "stage": infer_log_stage(message, step_name.as_deref()),
            "message": message,
            "job_id": job_id,
            "step_name": step_name,
        }));
    }
    let keep_from = entries.len().saturating_sub(120);
    json!({
        "latest_job_id": latest_job_id,
        "entries": entries.into_iter().skip(keep_from).collect::<Vec<_>>(),
    })
}

fn tail_log_lines(path: &Path, max_bytes: u64) -> Result<Vec<String>> {
    if !path.exists() || !path.is_file() {
        return Ok(Vec::new());
    }
    let mut file = fs::File::open(path)?;
    let file_size = file.metadata()?.len();
    let read_size = file_size.min(max_bytes);
    use std::io::{Read, Seek, SeekFrom};
    file.seek(SeekFrom::Start(file_size.saturating_sub(read_size)))?;
    let mut payload = Vec::new();
    file.read_to_end(&mut payload)?;
    let text = String::from_utf8_lossy(&payload);
    let mut lines: Vec<String> = text.lines().map(ToString::to_string).collect();
    if read_size < file_size && !lines.is_empty() {
        lines.remove(0);
    }
    Ok(lines)
}

fn log_field_regex(field_name: &str) -> &'static Regex {
    static CACHE: OnceLock<Mutex<HashMap<String, &'static Regex>>> = OnceLock::new();
    let cache = CACHE.get_or_init(|| Mutex::new(HashMap::new()));
    let mut guard = cache.lock().unwrap();
    if let Some(re) = guard.get(field_name) {
        return re;
    }
    let pattern = format!(r"\b{}=([^\s]+)", regex::escape(field_name));
    let leaked: &'static Regex = Box::leak(Box::new(Regex::new(&pattern).unwrap()));
    guard.insert(field_name.to_string(), leaked);
    leaked
}

fn extract_log_field(message: &str, field_name: &str) -> Option<String> {
    log_field_regex(field_name)
        .captures(message)
        .and_then(|caps| caps.get(1).map(|m| m.as_str().to_string()))
}

fn extract_step_name(message: &str) -> Option<String> {
    static STEP_NAME_RE: OnceLock<Regex> = OnceLock::new();
    let re = STEP_NAME_RE.get_or_init(|| Regex::new(r"\bstep_name=(.*?)(?:\s+\w+=|$)").unwrap());
    re.captures(message)
        .and_then(|caps| caps.get(1).map(|m| m.as_str().trim().to_string()))
        .filter(|value| !value.is_empty())
}

fn infer_log_stage(message: &str, step_name: Option<&str>) -> &'static str {
    let searchable = format!("{} {}", step_name.unwrap_or(""), message).to_lowercase();
    if searchable.contains("download") {
        "download"
    } else if searchable.contains("transcrib") || searchable.contains("whisper") {
        "transcription"
    } else if searchable.contains("chapter") {
        "chapters"
    } else if searchable.contains("identifying ads")
        || searchable.contains("classif")
        || searchable.contains("identification")
        || searchable.contains("boundary")
        || searchable.contains("modelcall")
        || searchable.contains("model call")
        || searchable.contains("llm")
    {
        "classification"
    } else if searchable.contains("processing audio")
        || searchable.contains("audio processor")
        || searchable.contains("ffmpeg")
        || searchable.contains("removed segment")
        || searchable.contains("processed audio")
    {
        "audio"
    } else if searchable.contains("[job_status")
        || searchable.contains("starting processing")
        || searchable.contains("processing complete")
        || searchable.contains("cancel")
        || searchable.contains("unexpected error")
        || searchable.contains("failed")
    {
        "job"
    } else {
        "general"
    }
}

fn build_file_debug(path_value: Option<&str>) -> Value {
    let Some(path_value) = path_value else {
        return json!({
            "path": Value::Null,
            "absolute_path": Value::Null,
            "exists": false,
            "is_file": false,
            "size_bytes": Value::Null,
        });
    };
    let path = Path::new(path_value);
    match fs::metadata(path) {
        Ok(metadata) => json!({
            "path": path_value,
            "absolute_path": path.canonicalize().ok().map(|path| path.to_string_lossy().to_string()),
            "exists": true,
            "is_file": metadata.is_file(),
            "size_bytes": if metadata.is_file() { Some(metadata.len()) } else { None },
        }),
        Err(error) => json!({
            "path": path_value,
            "absolute_path": path.canonicalize().ok().map(|path| path.to_string_lossy().to_string()),
            "exists": false,
            "is_file": false,
            "size_bytes": Value::Null,
            "error": error.to_string(),
        }),
    }
}

fn probe_audio_path(input: &Path) -> Result<ProbeResponse> {
    let output = Command::new("ffprobe")
        .arg("-v")
        .arg("error")
        .arg("-show_entries")
        .arg("format=duration")
        .arg("-of")
        .arg("json")
        .arg(input)
        .output()
        .with_context(|| "failed to run ffprobe")?;

    if !output.status.success() {
        return Err(anyhow!(
            "ffprobe failed with status {}: {}",
            output.status,
            String::from_utf8_lossy(&output.stderr).trim()
        ));
    }

    let payload: Value =
        serde_json::from_slice(&output.stdout).context("failed to parse ffprobe JSON")?;
    let duration_seconds = payload
        .get("format")
        .and_then(|format| format.get("duration"))
        .and_then(Value::as_str)
        .ok_or_else(|| anyhow!("ffprobe response did not include format.duration"))?
        .parse::<f64>()
        .context("ffprobe duration was not a number")?;

    if !duration_seconds.is_finite() || duration_seconds < 0.0 {
        return Err(anyhow!("ffprobe duration was invalid"));
    }

    Ok(ProbeResponse {
        duration_ms: (duration_seconds * 1000.0).round() as u64,
    })
}

fn cut_audio(args: AudioCutArgs) -> Result<OkResponse> {
    let duration_ms = probe_audio_path(&args.input)?.duration_ms;
    let windows = read_windows_ms(&args.windows_json)?;
    let encoding = EncodingOptions::new(
        args.encoding.clone(),
        args.cbr_bitrate_bps,
        args.vbr_quality,
    );

    match args.mode {
        CutMode::Exact => {
            cut_audio_simple(&args.input, &args.output, &windows, duration_ms, &encoding)?
        }
        CutMode::Fade => cut_audio_with_fade(
            &args.input,
            &args.output,
            &windows,
            duration_ms,
            args.fade_ms,
            &encoding,
        )?,
    }

    Ok(OkResponse { ok: true })
}

const BLEEP_MAX_WINDOWS_PER_PASS: usize = 96;

fn bleep_audio(args: AudioBleepArgs) -> Result<OkResponse> {
    let duration_ms = probe_audio_path(&args.input)?.duration_ms;
    let windows = read_windows_ms(&args.windows_json)?;
    if windows.is_empty() {
        fs::copy(&args.input, &args.output).with_context(|| "failed to copy input audio")?;
        return Ok(OkResponse { ok: true });
    }
    let encoding = EncodingOptions::new(
        args.encoding.clone(),
        args.cbr_bitrate_bps,
        args.vbr_quality,
    );

    let chunks: Vec<&[(u64, u64)]> = windows.chunks(BLEEP_MAX_WINDOWS_PER_PASS).collect();
    let total_passes = chunks.len();

    if total_passes == 1 {
        apply_bleep_pass_mp3(
            &args.input,
            &args.output,
            chunks[0],
            duration_ms,
            args.beep_frequency_hz,
            args.beep_volume,
            args.duck_volume,
            args.fade_ms,
            &encoding,
        )?;
        return Ok(OkResponse { ok: true });
    }

    // Multi-pass: keep intermediates lossless (WAV) so only the final mp3 encode is lossy.
    let temp_dir = tempfile::tempdir().with_context(|| "failed to create temp directory")?;
    let mut current_input = args.input.clone();
    for (idx, chunk) in chunks.iter().enumerate() {
        let is_last = idx == total_passes - 1;
        if is_last {
            apply_bleep_pass_mp3(
                &current_input,
                &args.output,
                chunk,
                duration_ms,
                args.beep_frequency_hz,
                args.beep_volume,
                args.duck_volume,
                args.fade_ms,
                &encoding,
            )?;
        } else {
            let pass_output = temp_dir.path().join(format!("bleep_pass_{idx}.wav"));
            apply_bleep_pass_wav(
                &current_input,
                &pass_output,
                chunk,
                duration_ms,
                args.beep_frequency_hz,
                args.beep_volume,
                args.duck_volume,
                args.fade_ms,
            )?;
            current_input = pass_output;
        }
    }
    Ok(OkResponse { ok: true })
}

// Build the bleep filter graph using:
//   * Voice: a chain of per-window `volume` filters with `enable=` so each
//     filter only fires inside its own zone. Each filter ramps the gain
//     between 1.0 and `duck` over `fade_ms` so window edges crossfade
//     instead of cutting abruptly (which produced audible clicks).
//   * Beep: one `sine` source per window, faded in/out and delayed to the
//     window start, then `amix` of all beep sources with the ducked voice.
// Both sides have matching crossfade curves so the voice attenuating and the
// beep coming in happen smoothly across the fade window.
fn bleep_filter_complex(
    windows: &[(u64, u64)],
    duration_ms: u64,
    beep_frequency_hz: u32,
    beep_volume: f32,
    duck_volume: f32,
    fade_ms: u32,
) -> String {
    let fade_s = (f64::from(fade_ms.max(1))) / 1000.0;
    let duration_s = duration_ms as f64 / 1000.0;

    // Expand each window by fade_ms on each side, then merge so adjacent
    // fade zones don't overlap and double-attenuate the voice.
    let zones: Vec<(f64, f64)> = {
        let fade_ms_u64 = u64::from(fade_ms);
        let expanded: Vec<(u64, u64)> = windows
            .iter()
            .map(|&(s, e)| (s.saturating_sub(fade_ms_u64), e.saturating_add(fade_ms_u64)))
            .collect();
        merge_windows(expanded)
            .into_iter()
            .map(|(zs, ze)| {
                (
                    (zs as f64 / 1000.0).max(0.0),
                    (ze as f64 / 1000.0).min(duration_s),
                )
            })
            .filter(|(zs, ze)| ze > zs)
            .collect()
    };

    let voice_coef = (1.0 - f64::from(duck_volume)).clamp(0.0, 1.0);

    let voice_filters: Vec<String> = zones
        .iter()
        .map(|&(zs, ze)| {
            // trapezoidal bump: 0 outside the zone, ramps 0→1 over fade_s at the
            // leading edge, holds at 1, ramps 1→0 at the trailing edge.
            format!(
                "volume='1-{coef:.4}*max(0,min(1,min((t-{zs:.4})/{r:.5},({ze:.4}-t)/{r:.5})))':eval=frame:enable='between(t,{zs:.4},{ze:.4})'",
                coef = voice_coef,
                zs = zs,
                ze = ze,
                r = fade_s,
            )
        })
        .collect();

    let voice_chain = if voice_filters.is_empty() {
        "[0:a]anull[ducked]".to_string()
    } else {
        format!("[0:a]{}[ducked]", voice_filters.join(","))
    };

    let mut beep_parts: Vec<String> = Vec::new();
    let mut beep_labels: Vec<String> = Vec::new();
    for (i, &(zs, ze)) in zones.iter().enumerate() {
        let beep_dur = ze - zs;
        if beep_dur <= 0.0 {
            continue;
        }
        let fade_actual = fade_s.min(beep_dur / 2.0);
        let fade_out_start = (beep_dur - fade_actual).max(0.0);
        let delay_ms_int = (zs * 1000.0).round() as u64;
        let label = format!("b{i}");
        beep_parts.push(format!(
            "sine=frequency={freq}:duration={dur:.4}:sample_rate=44100,\
             afade=t=in:d={fr:.5},\
             afade=t=out:st={fo:.5}:d={fr:.5},\
             volume={vol:.4},\
             adelay={delay}[{label}]",
            freq = beep_frequency_hz,
            dur = beep_dur,
            fr = fade_actual,
            fo = fade_out_start,
            vol = beep_volume,
            delay = delay_ms_int,
            label = label,
        ));
        beep_labels.push(format!("[{label}]"));
    }

    let mix = if beep_labels.is_empty() {
        "[ducked]anull[out]".to_string()
    } else {
        format!(
            "[ducked]{labels}amix=inputs={n}:duration=first:dropout_transition=0:normalize=0[out]",
            labels = beep_labels.join(""),
            n = beep_labels.len() + 1,
        )
    };

    let mut parts = vec![voice_chain];
    parts.extend(beep_parts);
    parts.push(mix);
    parts.join(";")
}

#[allow(clippy::too_many_arguments)]
fn apply_bleep_pass_mp3(
    input: &Path,
    output: &Path,
    windows: &[(u64, u64)],
    duration_ms: u64,
    beep_frequency_hz: u32,
    beep_volume: f32,
    duck_volume: f32,
    fade_ms: u32,
    encoding: &EncodingOptions,
) -> Result<()> {
    let filter = bleep_filter_complex(
        windows,
        duration_ms,
        beep_frequency_hz,
        beep_volume,
        duck_volume,
        fade_ms,
    );
    run_filtered_output(input, output, &filter, encoding)
}

#[allow(clippy::too_many_arguments)]
fn apply_bleep_pass_wav(
    input: &Path,
    output: &Path,
    windows: &[(u64, u64)],
    duration_ms: u64,
    beep_frequency_hz: u32,
    beep_volume: f32,
    duck_volume: f32,
    fade_ms: u32,
) -> Result<()> {
    let filter = bleep_filter_complex(
        windows,
        duration_ms,
        beep_frequency_hz,
        beep_volume,
        duck_volume,
        fade_ms,
    );
    let mut command = Command::new("ffmpeg");
    command
        .arg("-y")
        .arg("-v")
        .arg("error")
        .arg("-i")
        .arg(input)
        .arg("-filter_complex")
        .arg(&filter)
        .arg("-map")
        .arg("[out]")
        .arg("-codec:a")
        .arg("pcm_s16le")
        .arg(output);
    run_command(command, "ffmpeg filter (wav intermediate)")
}

fn split_audio(args: AudioSplitArgs) -> Result<SplitResponse> {
    if args.chunk_size_bytes == 0 {
        return Err(anyhow!("chunk_size_bytes must be a positive integer"));
    }

    fs::create_dir_all(&args.out_dir).with_context(|| "failed to create output directory")?;
    let duration_ms = probe_audio_path(&args.input)?.duration_ms;
    let file_size_bytes = fs::metadata(&args.input)
        .with_context(|| "failed to read input metadata")?
        .len();
    if file_size_bytes == 0 {
        return Err(anyhow!("cannot split zero-byte audio file"));
    }

    let chunk_ratio = args.chunk_size_bytes as f64 / file_size_bytes as f64;
    let chunk_duration_ms = ((duration_ms as f64 * chunk_ratio).ceil() as u64).max(1);
    let num_chunks = ((duration_ms as f64 / chunk_duration_ms as f64).ceil() as u64).max(1);

    let mut chunks = Vec::new();
    for index in 0..num_chunks {
        let start_offset_ms = index * chunk_duration_ms;
        if start_offset_ms >= duration_ms {
            break;
        }
        let end_offset_ms = duration_ms.min((index + 1) * chunk_duration_ms);
        let output_path = args.out_dir.join(format!("{index}.mp3"));
        trim_file(&args.input, &output_path, start_offset_ms, end_offset_ms)?;
        chunks.push(SplitChunk {
            path: output_path.to_string_lossy().into_owned(),
            offset_ms: start_offset_ms,
        });
    }

    Ok(SplitResponse { chunks })
}

fn cut_audio_with_fade(
    input: &Path,
    output: &Path,
    windows: &[(u64, u64)],
    duration_ms: u64,
    fade_ms: u32,
    encoding: &EncodingOptions,
) -> Result<()> {
    let fade_ms = fade_ms as u64;
    let mut streams = Vec::new();
    let mut last_end = 0;
    for (start_ms, end_ms) in windows {
        if *start_ms > last_end {
            streams.push((last_end, *start_ms, None));
        }
        streams.push((*start_ms, (*start_ms + fade_ms).min(*end_ms), Some("out")));
        streams.push((end_ms.saturating_sub(fade_ms), *end_ms, Some("in")));
        last_end = *end_ms;
    }
    if last_end < duration_ms {
        streams.push((last_end, duration_ms, None));
    }

    let mut filter_parts = Vec::new();
    let mut labels = Vec::new();
    for (index, (start_ms, end_ms, fade)) in streams.iter().enumerate() {
        if end_ms <= start_ms {
            continue;
        }
        let label = format!("a{index}");
        let mut part = format!(
            "[0:a]atrim=start={:.3}:end={:.3},asetpts=PTS-STARTPTS",
            *start_ms as f64 / 1000.0,
            *end_ms as f64 / 1000.0
        );
        if let Some(direction) = fade {
            part.push_str(&format!(
                ",afade=t={direction}:ss=0:d={:.3}",
                fade_ms as f64 / 1000.0
            ));
        }
        part.push_str(&format!("[{label}]"));
        filter_parts.push(part);
        labels.push(format!("[{label}]"));
    }

    if labels.is_empty() {
        return Err(anyhow!("no audio segments to keep after removal"));
    }

    filter_parts.push(format!(
        "{}concat=n={}:v=0:a=1[out]",
        labels.join(""),
        labels.len()
    ));
    run_filtered_output(input, output, &filter_parts.join(";"), encoding)
}

fn cut_audio_simple(
    input: &Path,
    output: &Path,
    windows: &[(u64, u64)],
    duration_ms: u64,
    encoding: &EncodingOptions,
) -> Result<()> {
    let keep_segments = keep_segments(windows, duration_ms);
    if keep_segments.is_empty() {
        return Err(anyhow!("no audio segments to keep after removal"));
    }

    let temp_dir = tempfile::tempdir().with_context(|| "failed to create temp directory")?;
    let mut segment_files = Vec::new();
    for (index, (start_ms, end_ms)) in keep_segments.iter().enumerate() {
        let segment_path = temp_dir.path().join(format!("segment_{index}.wav"));
        trim_file_lossless(input, &segment_path, *start_ms, *end_ms)?;
        segment_files.push(segment_path);
    }

    let concat_list_path = temp_dir.path().join("concat_list.txt");
    let mut concat_list =
        fs::File::create(&concat_list_path).with_context(|| "failed to create concat list")?;
    for segment_file in segment_files {
        writeln!(concat_list, "file '{}'", segment_file.display())
            .with_context(|| "failed to write concat list")?;
    }

    let mut command = Command::new("ffmpeg");
    command
        .arg("-y")
        .arg("-v")
        .arg("error")
        .arg("-f")
        .arg("concat")
        .arg("-safe")
        .arg("0")
        .arg("-i")
        .arg(concat_list_path)
        .arg("-codec:a")
        .arg("libmp3lame");
    add_encoding_args(&mut command, encoding);
    command.arg(output);
    run_command(command, "ffmpeg concat")
}

fn trim_file(input: &Path, output: &Path, start_ms: u64, end_ms: u64) -> Result<()> {
    let duration_ms = end_ms.saturating_sub(start_ms);
    if duration_ms == 0 {
        return Ok(());
    }

    let mut command = Command::new("ffmpeg");
    command
        .arg("-y")
        .arg("-v")
        .arg("error")
        .arg("-ss")
        .arg(format!("{:.3}", start_ms as f64 / 1000.0))
        .arg("-t")
        .arg(format!("{:.3}", duration_ms as f64 / 1000.0))
        .arg("-i")
        .arg(input)
        .arg("-codec:a")
        .arg("copy")
        .arg("-vn")
        .arg(output);
    run_command(command, "ffmpeg trim")
}

fn trim_file_lossless(input: &Path, output: &Path, start_ms: u64, end_ms: u64) -> Result<()> {
    let duration_ms = end_ms.saturating_sub(start_ms);
    let mut command = Command::new("ffmpeg");
    command
        .arg("-y")
        .arg("-v")
        .arg("error")
        .arg("-ss")
        .arg(format!("{:.3}", start_ms as f64 / 1000.0))
        .arg("-t")
        .arg(format!("{:.3}", duration_ms as f64 / 1000.0))
        .arg("-i")
        .arg(input)
        .arg("-codec:a")
        .arg("pcm_s16le")
        .arg("-vn");
    command.arg(output);
    run_command(command, "ffmpeg segment")
}

fn run_filtered_output(
    input: &Path,
    output: &Path,
    filter: &str,
    encoding: &EncodingOptions,
) -> Result<()> {
    let mut command = Command::new("ffmpeg");
    command
        .arg("-y")
        .arg("-v")
        .arg("error")
        .arg("-i")
        .arg(input)
        .arg("-filter_complex")
        .arg(filter)
        .arg("-map")
        .arg("[out]")
        .arg("-codec:a")
        .arg("libmp3lame");
    add_encoding_args(&mut command, encoding);
    command.arg(output);
    run_command(command, "ffmpeg filter")
}

fn run_command(mut command: Command, label: &str) -> Result<()> {
    let output = command
        .output()
        .with_context(|| format!("failed to run {label}"))?;
    if !output.status.success() {
        return Err(anyhow!(
            "{label} failed with status {}: {}",
            output.status,
            String::from_utf8_lossy(&output.stderr).trim()
        ));
    }
    Ok(())
}

fn add_encoding_args(command: &mut Command, encoding: &EncodingOptions) {
    match &encoding.mode {
        Encoding::Vbr => {
            command
                .arg("-q:a")
                .arg(encoding.vbr_quality.unwrap_or(2).to_string());
        }
        Encoding::Cbr => {
            command
                .arg("-b:a")
                .arg(encoding.cbr_bitrate_bps.unwrap_or(192_000).to_string());
        }
    }
}

fn read_windows_ms(path: &Path) -> Result<Vec<(u64, u64)>> {
    let data = fs::read_to_string(path).with_context(|| "failed to read windows JSON")?;
    let windows: Vec<WindowMs> =
        serde_json::from_str(&data).with_context(|| "failed to parse windows JSON")?;
    Ok(merge_windows(
        windows
            .into_iter()
            .filter_map(|WindowMs(start, end)| (end > start).then_some((start, end)))
            .collect(),
    ))
}

fn merge_windows(mut windows: Vec<(u64, u64)>) -> Vec<(u64, u64)> {
    windows.sort_by_key(|window| window.0);
    let mut merged: Vec<(u64, u64)> = Vec::new();
    for (start, end) in windows {
        match merged.last_mut() {
            Some((_, prev_end)) if start <= *prev_end => {
                *prev_end = (*prev_end).max(end);
            }
            _ => merged.push((start, end)),
        }
    }
    merged
}

fn keep_segments(windows: &[(u64, u64)], duration_ms: u64) -> Vec<(u64, u64)> {
    let mut keep = Vec::new();
    let mut last_end = 0;
    for (start, end) in windows {
        if *start > last_end {
            keep.push((last_end, *start));
        }
        last_end = *end;
    }
    if last_end < duration_ms {
        keep.push((last_end, duration_ms));
    }
    keep
}

fn normalize_word_timestamps(args: TranscriptNormalizeArgs) -> Result<OkResponse> {
    let data = fs::read_to_string(&args.input)
        .with_context(|| "failed to read transcript word timestamp artifact")?;
    let payload: Value =
        serde_json::from_str(&data).with_context(|| "failed to parse transcript JSON")?;
    let Value::Array(raw_segments) = payload else {
        fs::write(&args.output, b"null")
            .with_context(|| "failed to write normalized transcript JSON")?;
        return Ok(OkResponse { ok: true });
    };

    let mut normalized_segments = Vec::new();
    for raw_segment in raw_segments {
        let Value::Object(segment_map) = raw_segment else {
            continue;
        };
        let Some(sequence_num) = segment_map.get("sequence_num").and_then(value_to_i64) else {
            continue;
        };
        let Some(Value::Array(words)) = segment_map.get("words") else {
            continue;
        };

        let mut normalized_words = Vec::new();
        for word_payload in words {
            let Value::Object(word_map) = word_payload else {
                continue;
            };
            let Some(raw_word) = word_map.get("word") else {
                continue;
            };
            let Some(word) = value_to_string(raw_word) else {
                continue;
            };
            let Some(start) = word_map.get("start").and_then(value_to_f64) else {
                continue;
            };
            let Some(end) = word_map.get("end").and_then(value_to_f64) else {
                continue;
            };
            if end < start {
                continue;
            }
            let score = word_map.get("score").and_then(value_to_f64);
            normalized_words.push(NormalizedWord {
                word,
                start,
                end,
                score,
            });
        }

        if !normalized_words.is_empty() {
            normalized_segments.push(NormalizedWordSegment {
                sequence_num,
                words: normalized_words,
            });
        }
    }

    let normalized_payload = if normalized_segments.is_empty() {
        Value::Null
    } else {
        serde_json::to_value(normalized_segments)?
    };
    fs::write(&args.output, serde_json::to_vec(&normalized_payload)?)
        .with_context(|| "failed to write normalized transcript JSON")?;
    Ok(OkResponse { ok: true })
}

fn export_word_timestamps(args: TranscriptExportArgs) -> Result<TranscriptExportResponse> {
    let conn = Connection::open_with_flags(
        &args.db,
        OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_URI,
    )
    .with_context(|| "failed to open sqlite database read-only")?;
    conn.busy_timeout(std::time::Duration::from_secs(30))
        .with_context(|| "failed to set sqlite busy timeout")?;
    conn.pragma_update(None, "query_only", true)
        .with_context(|| "failed to enable sqlite query_only mode")?;

    let raw_json: Option<String> = conn
        .query_row(
            "SELECT transcript_word_timestamps FROM post WHERE id = ?1",
            [args.post_id],
            |row| row.get(0),
        )
        .with_context(|| "failed to query transcript_word_timestamps")?;

    let transcript_word_timestamps = match raw_json {
        Some(raw_json) => serde_json::from_str(&raw_json)
            .with_context(|| "stored transcript_word_timestamps was not valid JSON")?,
        None => Value::Null,
    };

    Ok(TranscriptExportResponse {
        transcript_word_timestamps,
    })
}

fn render_feed(args: FeedRenderArgs) -> Result<XmlResponse> {
    let conn = open_readonly_sqlite(&args.db)?;
    let feed = conn
        .query_row(
            "SELECT id, title, description, image_url, last_changed_at FROM feed WHERE id = ?1",
            [args.feed_id],
            |row| {
                Ok(FeedRow {
                    id: row.get(0)?,
                    title: row.get(1)?,
                    description: row.get(2)?,
                    image_url: row.get(3)?,
                    last_changed_at: row.get(4)?,
                })
            },
        )
        .with_context(|| "failed to query feed")?;

    let posts = if args.include_unprocessed {
        // Hide posts whose first processing pass is still queued or running:
        // podcast clients (notably Pocket Casts) cache ID3 metadata on first
        // parse and won't re-read after we add chapter tags. Mirrors the
        // Python filter in src/app/feeds.py::generate_feed_xml.
        query_posts(
            &conn,
            "SELECT NULL, title, guid, processed_audio_path, description, release_date, duration, image_url, chapter_data \
             FROM post \
             WHERE feed_id = ?1 \
               AND (processed_audio_path IS NOT NULL \
                    OR NOT EXISTS (SELECT 1 FROM processing_job \
                                   WHERE processing_job.post_guid = post.guid \
                                     AND processing_job.status IN ('pending','running'))) \
             ORDER BY release_date DESC, id DESC",
            [feed.id],
        )?
    } else {
        query_posts(
            &conn,
            "SELECT NULL, title, guid, processed_audio_path, description, release_date, duration, image_url, chapter_data \
             FROM post WHERE feed_id = ?1 AND whitelisted = 1 AND processed_audio_path IS NOT NULL \
             ORDER BY release_date DESC, id DESC",
            [feed.id],
        )?
    };

    let link = append_feed_token(
        &format!("{}/feed/{}", args.base_url.trim_end_matches('/'), feed.id),
        args.feed_token.as_deref(),
        args.feed_secret.as_deref(),
    );
    let feed_title = format!("[podly] {}", feed.title);
    let last_build_date = format_rfc2822(feed.last_changed_at.as_deref());
    let xml = build_rss_xml(RssBuildArgs {
        title: &feed_title,
        link: &link,
        description: feed.description.as_deref().unwrap_or(""),
        image_url: feed.image_url.as_deref(),
        image_title: &feed.title,
        last_build_date: &last_build_date,
        base_url: &args.base_url,
        feed_token: args.feed_token.as_deref(),
        feed_secret: args.feed_secret.as_deref(),
        posts: &posts,
        prepend_feed_title: false,
    });
    Ok(XmlResponse { xml })
}

struct PostListRow {
    id: i64,
    guid: String,
    title: String,
    description: Option<String>,
    release_date: Option<String>,
    duration: Value,
    whitelisted: bool,
    processed_audio_path: Option<String>,
    unprocessed_audio_path: Option<String>,
    download_url: String,
    image_url: Option<String>,
    download_count: Option<i64>,
    chapter_data: Option<String>,
}

fn render_feed_posts(args: PostsFeedListArgs) -> Result<Value> {
    let conn = open_readonly_sqlite(&args.db)?;

    // Mirror Flask's Feed.query.get_or_404: signal "not found" via a
    // distinct envelope rather than an error so the Python wrapper can
    // surface a 404 without parsing stderr.
    let feed_exists: Option<i64> = conn
        .query_row("SELECT id FROM feed WHERE id = ?1", [args.feed_id], |row| {
            row.get(0)
        })
        .optional()?;
    if feed_exists.is_none() {
        return Ok(json!({ "not_found": true }));
    }

    let page = args.page.max(1);
    let page_size = args.page_size.clamp(1, 200);
    let offset = (page - 1) * page_size;

    let where_clause = if args.whitelisted_only {
        "WHERE feed_id = ?1 AND whitelisted = 1"
    } else {
        "WHERE feed_id = ?1"
    };

    let whitelisted_total: i64 = conn.query_row(
        "SELECT COUNT(*) FROM post WHERE feed_id = ?1 AND whitelisted = 1",
        [args.feed_id],
        |row| row.get(0),
    )?;
    // Reuse the filtered count rather than running the identical query twice.
    // Keep separate indexed counts for the unfiltered case.
    let total: i64 = if args.whitelisted_only {
        whitelisted_total
    } else {
        conn.query_row(
            "SELECT COUNT(*) FROM post WHERE feed_id = ?1",
            [args.feed_id],
            |row| row.get(0),
        )?
    };

    // Explicit column list — never SELECT *. transcript_word_timestamps,
    // bleep_windows, and refined_ad_boundaries are NOT in this list, which
    // is the whole point of porting the endpoint: we never page those huge
    // JSON blobs into the Python long-lived heap.
    let sql = format!(
        "SELECT id, guid, title, description, release_date, duration, whitelisted, \
                processed_audio_path, unprocessed_audio_path, download_url, image_url, \
                download_count, chapter_data \
         FROM post {where_clause} \
         ORDER BY (release_date IS NULL), release_date DESC, id DESC \
         LIMIT ?2 OFFSET ?3"
    );

    let mut stmt = conn.prepare(&sql)?;
    let rows = stmt.query_map(rusqlite::params![args.feed_id, page_size, offset], |row| {
        let whitelisted_int: i64 = row.get(6)?;
        Ok(PostListRow {
            id: row.get(0)?,
            guid: row.get(1)?,
            title: row.get(2)?,
            description: row.get(3)?,
            release_date: row.get(4)?,
            duration: get_numeric_json_value(row, 5)?,
            whitelisted: whitelisted_int != 0,
            processed_audio_path: row.get(7)?,
            unprocessed_audio_path: row.get(8)?,
            download_url: row.get(9)?,
            image_url: row.get(10)?,
            download_count: row.get(11)?,
            chapter_data: row.get(12)?,
        })
    })?;
    // Consume one row at a time instead of retaining the whole source page
    // alongside the response, especially descriptions and chapter JSON.
    let items: Vec<Value> = rows
        .map(|post| {
            let post = post?;
            let podly_html = build_post_list_description_html(
                post.description.as_deref(),
                post.chapter_data.as_deref(),
            );
            Ok(json!({
                "id": post.id,
                "guid": post.guid,
                "title": post.title,
                "description": post.description,
                "podly_description_html": podly_html,
                "release_date": post.release_date.as_deref().map(format_release_date_iso8601),
                "duration": post.duration,
                "whitelisted": post.whitelisted,
                "has_processed_audio": post.processed_audio_path.is_some(),
                "has_unprocessed_audio": post.unprocessed_audio_path.is_some(),
                "download_url": post.download_url,
                "image_url": post.image_url,
                "download_count": post.download_count,
            }))
        })
        .collect::<rusqlite::Result<_>>()?;

    let total_pages = if total > 0 {
        (total + page_size - 1) / page_size
    } else {
        0
    };

    let mut envelope = json!({
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": total_pages,
        "whitelisted_total": whitelisted_total,
    });
    // json! borrows its expressions; insert the array by value to avoid
    // cloning the entire page into the envelope.
    envelope["items"] = Value::Array(items);
    Ok(envelope)
}

// ===== Admin costs endpoints =====
//
// Mirrors `api_admin_costs` and `api_admin_costs_calls` in
// src/app/routes/cost_routes.py. Python pre-resolves LiteLLM per-token rates
// (including the Flex 0.5x discount and the cached_input→input fallback) and
// passes them in via --rates-json so the sidecar never has to know about
// LiteLLM. Keep the payload shape byte-identical to the Python responses —
// the parity test in tests/test_cost_routes_rust_parity.py will fail loudly
// if you drift.

#[derive(Deserialize)]
struct CostRatesPayload {
    rates: HashMap<String, ModelRate>,
    #[serde(default)]
    whisper_rate_per_hour: f64,
    #[serde(default)]
    ina_rate_per_hour: f64,
    #[serde(default)]
    legacy_rate_per_hour: f64,
}

#[derive(Deserialize, Clone)]
struct ModelRate {
    #[serde(default)]
    input: f64,
    #[serde(default)]
    cached_input: f64,
    #[serde(default)]
    output: f64,
}

struct CostsModelCallRow {
    id: i64,
    post_id: Option<i64>,
    model_name: String,
    status: String,
    timestamp: Option<String>,
    retry_attempts: i64,
    service_tier: Option<String>,
    prompt: Option<String>,
    prompt_tokens: Option<i64>,
    cached_prompt_tokens: Option<i64>,
    completion_tokens: Option<i64>,
    total_tokens: Option<i64>,
}

fn rate_key(model_name: &str, service_tier: Option<&str>) -> String {
    let tier = service_tier
        .map(|t| t.trim().to_ascii_lowercase())
        .filter(|t| !t.is_empty())
        .unwrap_or_else(|| "default".to_string());
    format!("{model_name}|{tier}")
}

fn is_ina_call(model_name: &str) -> bool {
    model_name.starts_with("ina:")
}

fn is_whisper_call(model_name: &str, prompt: Option<&str>) -> bool {
    let lower = model_name.to_ascii_lowercase();
    if lower.contains("whisper") {
        return true;
    }
    if prompt == Some("Whisper transcription job") {
        return true;
    }
    lower.starts_with("local_")
}

fn is_billable_llm_call(status: &str, model_name: &str, prompt: Option<&str>) -> bool {
    if status != "success" {
        return false;
    }
    if is_ina_call(model_name) {
        return false;
    }
    if is_whisper_call(model_name, prompt) {
        return false;
    }
    true
}

fn model_call_cost(call: &CostsModelCallRow, rates: &HashMap<String, ModelRate>) -> f64 {
    let prompt_tokens = call.prompt_tokens.unwrap_or(0).max(0) as f64;
    let cached_prompt_tokens = call.cached_prompt_tokens.unwrap_or(0).max(0) as f64;
    let completion_tokens = call.completion_tokens.unwrap_or(0).max(0) as f64;
    if prompt_tokens <= 0.0 && cached_prompt_tokens <= 0.0 && completion_tokens <= 0.0 {
        return 0.0;
    }
    let key = rate_key(&call.model_name, call.service_tier.as_deref());
    let rate = match rates.get(&key) {
        Some(r) => r,
        None => return 0.0,
    };
    // OpenAI reports cached tokens as a subset of prompt_tokens.
    let uncached_prompt_tokens = (prompt_tokens - cached_prompt_tokens).max(0.0);
    uncached_prompt_tokens * rate.input
        + cached_prompt_tokens * rate.cached_input
        + completion_tokens * rate.output
}

fn load_cost_rates(path: &Path) -> Result<CostRatesPayload> {
    let raw = fs::read_to_string(path).with_context(|| "failed to read cost rates JSON")?;
    let payload: CostRatesPayload =
        serde_json::from_str(&raw).with_context(|| "failed to parse cost rates JSON")?;
    Ok(payload)
}

fn month_range(year: i32, month: u32) -> Result<(String, String)> {
    use chrono::{NaiveDate, NaiveDateTime, NaiveTime};
    let start_date = NaiveDate::from_ymd_opt(year, month, 1)
        .ok_or_else(|| anyhow!("Invalid year/month {year}/{month}"))?;
    let next_month = if month == 12 {
        NaiveDate::from_ymd_opt(year + 1, 1, 1)
    } else {
        NaiveDate::from_ymd_opt(year, month + 1, 1)
    }
    .ok_or_else(|| anyhow!("Invalid year/month {year}/{month}"))?;
    let last_day = next_month.pred_opt().unwrap();
    let start = NaiveDateTime::new(start_date, NaiveTime::from_hms_opt(0, 0, 0).unwrap());
    let end = NaiveDateTime::new(last_day, NaiveTime::from_hms_opt(23, 59, 59).unwrap());
    Ok((
        start.format("%Y-%m-%d %H:%M:%S").to_string(),
        end.format("%Y-%m-%d %H:%M:%S").to_string(),
    ))
}

fn render_admin_costs(args: CostsRenderAdminArgs) -> Result<Value> {
    let conn = open_readonly_sqlite(&args.db)?;
    let rates_payload = load_cost_rates(&args.rates_json)?;
    let rates = &rates_payload.rates;
    let (month_start, month_end) = month_range(args.year, args.month)?;

    // --- latest_completed_jobs_by_guid ---
    let mut latest_completed: HashMap<String, String> = HashMap::new(); // guid -> completed_at (raw string)
    {
        let mut stmt = conn.prepare(
            "SELECT post_guid, completed_at FROM processing_job \
             WHERE status = 'completed' AND completed_at IS NOT NULL \
               AND completed_at >= ?1 AND completed_at <= ?2",
        )?;
        let rows = stmt.query_map([&month_start, &month_end], |row| {
            let guid: String = row.get(0)?;
            let completed_at: String = row.get(1)?;
            Ok((guid, completed_at))
        })?;
        for row in rows {
            let (guid, completed_at) = row?;
            let entry = latest_completed
                .entry(guid)
                .or_insert_with(|| completed_at.clone());
            if completed_at > *entry {
                *entry = completed_at;
            }
        }
    }
    let guids: Vec<String> = latest_completed.keys().cloned().collect();

    // --- Users ordered by username (lowercase normalized in DB already) ---
    let users: Vec<(i64, String, String, String, Option<String>)> = {
        let mut stmt = conn.prepare(
            "SELECT id, username, role, feed_subscription_status, stripe_subscription_id \
             FROM users ORDER BY username",
        )?;
        let rows = stmt.query_map([], |row| {
            Ok((
                row.get::<_, i64>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, String>(2)?,
                row.get::<_, String>(3)?,
                row.get::<_, Option<String>>(4)?,
            ))
        })?;
        rows.collect::<std::result::Result<Vec<_>, _>>()?
    };

    // --- Feeds ---
    let feeds: Vec<(i64, Option<String>)> = {
        let mut stmt = conn.prepare("SELECT id, title FROM feed")?;
        let rows = stmt.query_map([], |row| {
            Ok((row.get::<_, i64>(0)?, row.get::<_, Option<String>>(1)?))
        })?;
        rows.collect::<std::result::Result<Vec<_>, _>>()?
    };

    // --- feed_supporter -> user_feed / feed_user maps ---
    let mut feed_subscriber_count: HashMap<i64, i64> = HashMap::new();
    let mut feed_user_map: HashMap<i64, Vec<i64>> = HashMap::new();
    let mut user_feed_map: HashMap<i64, Vec<i64>> = HashMap::new();
    {
        let mut stmt = conn.prepare("SELECT feed_id, user_id FROM feed_supporter")?;
        let rows = stmt.query_map([], |row| Ok((row.get::<_, i64>(0)?, row.get::<_, i64>(1)?)))?;
        for row in rows {
            let (feed_id, user_id) = row?;
            *feed_subscriber_count.entry(feed_id).or_insert(0) += 1;
            feed_user_map.entry(feed_id).or_default().push(user_id);
            user_feed_map.entry(user_id).or_default().push(feed_id);
        }
    }

    // --- Posts for these guids ---
    let mut posts_by_guid: HashMap<String, (i64, i64, f64)> = HashMap::new(); // guid -> (id, feed_id, duration)
    let mut post_ids: Vec<i64> = Vec::new();
    if !guids.is_empty() {
        let placeholders: String = (0..guids.len()).map(|_| "?").collect::<Vec<_>>().join(",");
        let sql =
            format!("SELECT id, guid, feed_id, duration FROM post WHERE guid IN ({placeholders})");
        let mut stmt = conn.prepare(&sql)?;
        let params: Vec<&dyn rusqlite::ToSql> =
            guids.iter().map(|g| g as &dyn rusqlite::ToSql).collect();
        let rows = stmt.query_map(params.as_slice(), |row| {
            let id: i64 = row.get(0)?;
            let guid: String = row.get(1)?;
            let feed_id: i64 = row.get(2)?;
            let duration: Option<f64> = get_duration_f64(row, 3)?;
            Ok((id, guid, feed_id, duration.unwrap_or(0.0)))
        })?;
        for row in rows {
            let (id, guid, feed_id, duration) = row?;
            posts_by_guid.insert(guid, (id, feed_id, duration));
            post_ids.push(id);
        }
    }

    // --- ad time per post (sum of (end-start) where label = 'ad') ---
    let mut ad_time_by_post: HashMap<i64, f64> = HashMap::new();
    if !post_ids.is_empty() {
        let placeholders: String = (0..post_ids.len())
            .map(|_| "?")
            .collect::<Vec<_>>()
            .join(",");
        let sql = format!(
            "SELECT transcript_segment.post_id, \
                    SUM(transcript_segment.end_time - transcript_segment.start_time) \
             FROM transcript_segment \
             JOIN identification \
               ON identification.transcript_segment_id = transcript_segment.id \
             WHERE identification.label = 'ad' \
               AND transcript_segment.post_id IN ({placeholders}) \
             GROUP BY transcript_segment.post_id"
        );
        let mut stmt = conn.prepare(&sql)?;
        let params: Vec<&dyn rusqlite::ToSql> =
            post_ids.iter().map(|p| p as &dyn rusqlite::ToSql).collect();
        let rows = stmt.query_map(params.as_slice(), |row| {
            Ok((row.get::<_, i64>(0)?, row.get::<_, f64>(1)?))
        })?;
        for row in rows {
            let (post_id, ad_time) = row?;
            ad_time_by_post.insert(post_id, ad_time);
        }
    }

    // --- Successful model_calls per post (used for aggregation only) ---
    let mut calls_by_post: HashMap<i64, Vec<CostsModelCallRow>> = HashMap::new();
    if !post_ids.is_empty() {
        let placeholders: String = (0..post_ids.len())
            .map(|_| "?")
            .collect::<Vec<_>>()
            .join(",");
        let sql = format!(
            "SELECT id, post_id, model_name, status, timestamp, retry_attempts, \
                    service_tier, prompt, prompt_tokens, cached_prompt_tokens, \
                    completion_tokens, total_tokens \
             FROM model_call \
             WHERE status = 'success' AND post_id IN ({placeholders}) \
             ORDER BY id ASC"
        );
        let mut stmt = conn.prepare(&sql)?;
        let params: Vec<&dyn rusqlite::ToSql> =
            post_ids.iter().map(|p| p as &dyn rusqlite::ToSql).collect();
        let rows = stmt.query_map(params.as_slice(), |row| {
            Ok(CostsModelCallRow {
                id: row.get(0)?,
                post_id: row.get(1)?,
                model_name: row.get(2)?,
                status: row.get(3)?,
                timestamp: row.get(4)?,
                retry_attempts: row.get::<_, Option<i64>>(5)?.unwrap_or(0),
                service_tier: row.get(6)?,
                prompt: row.get(7)?,
                prompt_tokens: row.get(8)?,
                cached_prompt_tokens: row.get(9)?,
                completion_tokens: row.get(10)?,
                total_tokens: row.get(11)?,
            })
        })?;
        for call_result in rows {
            let call = call_result?;
            if let Some(pid) = call.post_id {
                calls_by_post.entry(pid).or_default().push(call);
            }
        }
    }

    // --- Per-feed / per-user aggregation ---
    let mut user_costs: HashMap<i64, f64> = users.iter().map(|u| (u.0, 0.0)).collect();
    let mut feed_costs: HashMap<i64, f64> = feeds.iter().map(|f| (f.0, 0.0)).collect();
    let mut feed_llm_costs: HashMap<i64, f64> = feeds.iter().map(|f| (f.0, 0.0)).collect();
    let mut feed_whisper_costs: HashMap<i64, f64> = feeds.iter().map(|f| (f.0, 0.0)).collect();
    let mut feed_ina_costs: HashMap<i64, f64> = feeds.iter().map(|f| (f.0, 0.0)).collect();
    let mut feed_episode_counts: HashMap<i64, i64> = feeds.iter().map(|f| (f.0, 0)).collect();
    let mut total_audio_hours: f64 = 0.0;

    for guid in latest_completed.keys() {
        let (post_id, feed_id, cut_duration) = match posts_by_guid.get(guid) {
            Some(v) => *v,
            None => continue,
        };
        let ad_time = *ad_time_by_post.get(&post_id).unwrap_or(&0.0);
        let duration = cut_duration + ad_time;
        let duration_hours = if duration > 0.0 {
            duration / 3600.0
        } else {
            0.0
        };
        total_audio_hours += duration_hours;
        let post_calls = calls_by_post.get(&post_id);
        let llm_cost: f64 = post_calls
            .map(|calls| {
                calls
                    .iter()
                    .filter(|c| is_billable_llm_call(&c.status, &c.model_name, c.prompt.as_deref()))
                    .map(|c| model_call_cost(c, rates))
                    .sum()
            })
            .unwrap_or(0.0);
        let has_whisper = post_calls
            .map(|calls| {
                calls
                    .iter()
                    .any(|c| is_whisper_call(&c.model_name, c.prompt.as_deref()))
            })
            .unwrap_or(false);
        let has_ina = post_calls
            .map(|calls| calls.iter().any(|c| is_ina_call(&c.model_name)))
            .unwrap_or(false);
        let whisper_cost = if has_whisper {
            rates_payload.whisper_rate_per_hour * duration_hours
        } else {
            0.0
        };
        let ina_cost = if has_ina {
            rates_payload.ina_rate_per_hour * duration_hours
        } else {
            0.0
        };
        let total_episode_cost = llm_cost + whisper_cost + ina_cost;
        let subscriber_count = *feed_subscriber_count.get(&feed_id).unwrap_or(&0);
        let cost_per_sub = if subscriber_count > 0 && total_episode_cost > 0.0 {
            total_episode_cost / subscriber_count as f64
        } else {
            0.0
        };

        *feed_costs.entry(feed_id).or_insert(0.0) += total_episode_cost;
        *feed_llm_costs.entry(feed_id).or_insert(0.0) += llm_cost;
        *feed_whisper_costs.entry(feed_id).or_insert(0.0) += whisper_cost;
        *feed_ina_costs.entry(feed_id).or_insert(0.0) += ina_cost;
        *feed_episode_counts.entry(feed_id).or_insert(0) += 1;

        if let Some(user_ids) = feed_user_map.get(&feed_id) {
            for uid in user_ids {
                *user_costs.entry(*uid).or_insert(0.0) += cost_per_sub;
            }
        }
    }

    // --- Build users JSON ---
    // NOTE: the Python endpoint also fetches Stripe subscription amount via
    // billing_cache.fetch_subscription_amount. That requires Stripe API access
    // and a live cache, which the sidecar can't replicate. The Python wrapper
    // post-processes the Rust response to fill in subscription_amount_cents
    // for users whose stripe_subscription_id is non-null.
    let users_data: Vec<Value> = users
        .iter()
        .map(|(uid, username, role, sub_status, stripe_sub_id)| {
            let feed_count = user_feed_map.get(uid).map(|v| v.len()).unwrap_or(0);
            json!({
                "id": uid,
                "username": username,
                "role": role,
                "feed_count": feed_count,
                "subscription_status": sub_status,
                "stripe_subscription_id": stripe_sub_id,
                "subscription_amount_cents": Value::Null,
                "monthly_cost": round_to(*user_costs.get(uid).unwrap_or(&0.0), 4),
            })
        })
        .collect();

    // --- Build feeds JSON sorted by monthly_cost desc ---
    let mut feeds_data: Vec<Value> = feeds
        .iter()
        .map(|(fid, title)| {
            json!({
                "id": fid,
                "title": title,
                "subscriber_count": *feed_subscriber_count.get(fid).unwrap_or(&0),
                "episodes_this_month": *feed_episode_counts.get(fid).unwrap_or(&0),
                "monthly_cost": round_to(*feed_costs.get(fid).unwrap_or(&0.0), 4),
                "llm_cost": round_to(*feed_llm_costs.get(fid).unwrap_or(&0.0), 4),
                "whisper_cost": round_to(*feed_whisper_costs.get(fid).unwrap_or(&0.0), 4),
                "ina_cost": round_to(*feed_ina_costs.get(fid).unwrap_or(&0.0), 4),
            })
        })
        .collect();
    feeds_data.sort_by(|a, b| {
        let av = a
            .get("monthly_cost")
            .and_then(|v| v.as_f64())
            .unwrap_or(0.0);
        let bv = b
            .get("monthly_cost")
            .and_then(|v| v.as_f64())
            .unwrap_or(0.0);
        bv.partial_cmp(&av).unwrap_or(std::cmp::Ordering::Equal)
    });

    let total_cost: f64 = feed_costs.values().sum();
    let total_llm: f64 = feed_llm_costs.values().sum();
    let total_whisper: f64 = feed_whisper_costs.values().sum();
    let total_ina: f64 = feed_ina_costs.values().sum();

    Ok(json!({
        "year": args.year,
        "month": args.month,
        "total_cost": round_to(total_cost, 4),
        "cost_rate_per_hour": rates_payload.legacy_rate_per_hour,
        "whisper_cost_rate_per_hour": rates_payload.whisper_rate_per_hour,
        "ina_cost_rate_per_hour": rates_payload.ina_rate_per_hour,
        "total_llm_cost": round_to(total_llm, 4),
        "total_whisper_cost": round_to(total_whisper, 4),
        "total_ina_cost": round_to(total_ina, 4),
        "total_audio_hours": round_to(total_audio_hours, 2),
        "users": users_data,
        "feeds": feeds_data,
    }))
}

fn render_admin_costs_calls(args: CostsRenderCallsArgs) -> Result<Value> {
    let conn = open_readonly_sqlite(&args.db)?;
    let rates_payload = load_cost_rates(&args.rates_json)?;
    let rates = &rates_payload.rates;

    let page = args.page.max(1);
    let per_page = args.per_page.clamp(1, 200);
    let offset = (page - 1) * per_page;

    let total: i64 = conn.query_row("SELECT COUNT(*) FROM model_call", [], |row| row.get(0))?;

    let mut stmt = conn.prepare(
        "SELECT id, post_id, model_name, status, timestamp, retry_attempts, \
                service_tier, prompt, prompt_tokens, cached_prompt_tokens, \
                completion_tokens, total_tokens \
         FROM model_call \
         ORDER BY timestamp DESC \
         LIMIT ?1 OFFSET ?2",
    )?;
    let rows = stmt.query_map([per_page, offset], |row| {
        Ok(CostsModelCallRow {
            id: row.get(0)?,
            post_id: row.get(1)?,
            model_name: row.get(2)?,
            status: row.get(3)?,
            timestamp: row.get(4)?,
            retry_attempts: row.get::<_, Option<i64>>(5)?.unwrap_or(0),
            service_tier: row.get(6)?,
            prompt: row.get(7)?,
            prompt_tokens: row.get(8)?,
            cached_prompt_tokens: row.get(9)?,
            completion_tokens: row.get(10)?,
            total_tokens: row.get(11)?,
        })
    })?;
    let calls: Vec<CostsModelCallRow> = rows.collect::<std::result::Result<Vec<_>, _>>()?;

    let calls_data: Vec<Value> = calls
        .iter()
        .map(|c| {
            let estimated = if is_billable_llm_call(&c.status, &c.model_name, c.prompt.as_deref()) {
                round_to(model_call_cost(c, rates), 8)
            } else {
                0.0
            };
            json!({
                "id": c.id,
                "post_id": c.post_id,
                "model_name": c.model_name,
                "status": c.status,
                "timestamp": c.timestamp.as_deref().map(sqlite_datetime_to_iso),
                "retry_attempts": c.retry_attempts,
                "service_tier": c.service_tier,
                "prompt_tokens": c.prompt_tokens,
                "cached_prompt_tokens": c.cached_prompt_tokens,
                "completion_tokens": c.completion_tokens,
                "total_tokens": c.total_tokens,
                "estimated_cost": estimated,
            })
        })
        .collect();

    let pages = if total > 0 {
        (total + per_page - 1) / per_page
    } else {
        1
    };

    Ok(json!({
        "calls": calls_data,
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": pages,
    }))
}

fn build_post_list_description_html(
    description: Option<&str>,
    chapter_data: Option<&str>,
) -> String {
    let mut parts: Vec<String> = Vec::new();
    if let Some(desc) = description {
        if !desc.is_empty() {
            parts.push(normalize_feed_text(desc));
        }
    }
    let chapters = render_chapters(chapter_data);
    if !chapters.is_empty() {
        parts.push(chapters);
    }
    parts.join("\n")
}

fn format_release_date_iso8601(raw: &str) -> String {
    // SQLAlchemy DateTime(timezone=True) on SQLite stores as
    // 'YYYY-MM-DD HH:MM:SS[.ffffff]'. Python's datetime.isoformat() emits
    // 'YYYY-MM-DDTHH:MM:SS[.ffffff]'. Mirror that shape; if parsing fails
    // we pass the raw string through so the response stays consistent
    // with whatever Python would have produced.
    if let Ok(dt) = DateTime::parse_from_rfc3339(raw) {
        return dt.to_rfc3339();
    }
    if let Ok(naive) = NaiveDateTime::parse_from_str(raw, "%Y-%m-%d %H:%M:%S%.f") {
        return naive.format("%Y-%m-%dT%H:%M:%S%.f").to_string();
    }
    if let Ok(naive) = NaiveDateTime::parse_from_str(raw, "%Y-%m-%d %H:%M:%S") {
        return naive.format("%Y-%m-%dT%H:%M:%S").to_string();
    }
    raw.to_string()
}

fn write_chapters(args: ChaptersWriteArgs) -> Result<OkResponse> {
    let data =
        fs::read_to_string(&args.chapters_json).with_context(|| "failed to read chapters JSON")?;
    let mut chapters: Vec<ChapterPayload> =
        serde_json::from_str(&data).with_context(|| "failed to parse chapters JSON")?;
    if chapters.is_empty() {
        return Ok(OkResponse { ok: true });
    }
    chapters.sort_by_key(|chapter| chapter.start_time_ms);

    // Clamp chapter end_times to the real audio duration. Some readers
    // (notably Pocket Casts) silently drop chapters whose end_time exceeds
    // the file length, which can happen after ad-removal recalculation if
    // the cut math and ffmpeg's reported duration disagree by even a few ms.
    let audio_duration_ms_u64 = probe_audio_path(&args.audio).ok().map(|p| p.duration_ms);
    if let Some(duration_ms) = audio_duration_ms_u64 {
        let duration_u32 = duration_ms.min(u64::from(u32::MAX)) as u32;
        chapters.retain(|chapter| chapter.start_time_ms < duration_u32);
        for chapter in chapters.iter_mut() {
            if chapter.end_time_ms > duration_u32 {
                chapter.end_time_ms = duration_u32;
            }
        }
    }
    if chapters.is_empty() {
        return Ok(OkResponse { ok: true });
    }

    // Players (e.g. Apple Podcasts, Overcast) ignore chapter markup unless the
    // chapters cover the entire file. Extend the first chapter back to 0 and
    // the last chapter forward to the audio end so there are no gaps.
    if let Some(first) = chapters.first_mut() {
        if first.start_time_ms > 0 {
            first.start_time_ms = 0;
            if first.end_time_ms < first.start_time_ms {
                first.end_time_ms = first.start_time_ms;
            }
        }
    }
    if let Some(duration_ms) = audio_duration_ms_u64 {
        let duration_u32 = duration_ms.min(u64::from(u32::MAX)) as u32;
        if let Some(last) = chapters.last_mut() {
            if last.end_time_ms < duration_u32 {
                last.end_time_ms = duration_u32;
            }
        }
    }

    let mut tag = Tag::read_from_path(&args.audio).unwrap_or_else(|_| Tag::new());
    tag.remove("CHAP");
    tag.remove("CTOC");

    let total_duration_ms = audio_duration_ms_u64
        .map(|ms| ms.min(u64::from(u32::MAX)) as u32)
        .unwrap_or_else(|| {
            chapters
                .iter()
                .map(|chapter| chapter.end_time_ms)
                .max()
                .unwrap_or(0)
        });

    let mut chapter_ids = Vec::new();
    for (index, chapter) in chapters.into_iter().enumerate() {
        let element_id = format!("chp{index}");
        chapter_ids.push(element_id.clone());
        let title_frame = Frame::with_content("TIT2", Content::Text(chapter.title))
            .set_encoding(Some(Id3Encoding::UTF16));
        tag.add_frame(Id3Chapter {
            element_id,
            start_time: chapter.start_time_ms,
            end_time: chapter.end_time_ms,
            start_offset: 0xFFFF_FFFF,
            end_offset: 0xFFFF_FFFF,
            frames: vec![title_frame],
        });
    }

    tag.add_frame(TableOfContents {
        element_id: "toc".to_string(),
        top_level: true,
        ordered: true,
        elements: chapter_ids,
        frames: vec![],
    });

    // TLEN gives players the expected total duration; some readers (Pocket Casts)
    // discard chapters whose end_time exceeds the file duration when TLEN is absent.
    tag.remove("TLEN");
    if total_duration_ms > 0 {
        tag.add_frame(Frame::with_content(
            "TLEN",
            Content::Text(total_duration_ms.to_string()),
        ));
    }

    tag.write_to_path(&args.audio, Version::Id3v23)
        .with_context(|| "failed to write chapter ID3 tags")?;

    Ok(OkResponse { ok: true })
}

fn read_chapters(args: ChaptersReadArgs) -> Result<ChaptersReadResponse> {
    Ok(ChaptersReadResponse {
        chapters: read_chapters_from_audio(&args.audio)?,
    })
}

fn detect_chapter_ads(args: ChaptersDetectArgs) -> Result<ChaptersDetectResponse> {
    let chapters = read_chapters_from_audio(&args.audio)?;
    let filters = parse_filter_strings_csv(&args.filter_strings_csv);
    let (chapters_to_keep, chapters_to_remove) = filter_chapters_by_strings(chapters, &filters);
    let ad_segments = chapters_to_remove
        .iter()
        .map(|chapter| {
            (
                f64::from(chapter.start_time_ms) / 1000.0,
                f64::from(chapter.end_time_ms) / 1000.0,
            )
        })
        .collect();

    Ok(ChaptersDetectResponse {
        ad_segments,
        chapters_to_keep,
        chapters_to_remove,
    })
}

fn read_chapters_from_audio(audio: &Path) -> Result<Vec<ChapterResponseItem>> {
    let Ok(tag) = Tag::read_from_path(audio) else {
        return Ok(Vec::new());
    };

    let mut chapters: Vec<ChapterResponseItem> = tag
        .chapters()
        .map(|chapter| {
            let title = chapter
                .frames
                .iter()
                .find(|frame| frame.id() == "TIT2")
                .and_then(|frame| match frame.content() {
                    Content::Text(text) => Some(text.clone()),
                    _ => None,
                })
                .filter(|title| !title.is_empty())
                .unwrap_or_else(|| chapter.element_id.clone());

            ChapterResponseItem {
                element_id: chapter.element_id.clone(),
                title,
                start_time_ms: chapter.start_time,
                end_time_ms: chapter.end_time,
            }
        })
        .collect();
    chapters.sort_by_key(|chapter| chapter.start_time_ms);
    Ok(chapters)
}

fn parse_filter_strings_csv(filter_strings_csv: &str) -> Vec<String> {
    filter_strings_csv
        .split(',')
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(str::to_lowercase)
        .collect()
}

fn filter_chapters_by_strings(
    chapters: Vec<ChapterResponseItem>,
    filters: &[String],
) -> (Vec<ChapterResponseItem>, Vec<ChapterResponseItem>) {
    if filters.is_empty() {
        return (chapters, Vec::new());
    }

    chapters.into_iter().partition(|chapter| {
        let title = chapter.title.to_lowercase();
        !filters.iter().any(|filter| title.contains(filter))
    })
}

fn render_aggregate_feed(args: FeedRenderAggregateArgs) -> Result<XmlResponse> {
    let conn = open_readonly_sqlite(&args.db)?;
    let feed_ids: Vec<i64> = if args.require_auth {
        let mut stmt = conn.prepare("SELECT feed_id FROM feed_supporter WHERE user_id = ?1")?;
        let rows = stmt.query_map([args.user_id], |row| row.get(0))?;
        rows.collect::<std::result::Result<Vec<i64>, _>>()?
    } else {
        let mut stmt = conn.prepare("SELECT id FROM feed")?;
        let rows = stmt.query_map([], |row| row.get(0))?;
        rows.collect::<std::result::Result<Vec<i64>, _>>()?
    };

    let mut posts = Vec::new();
    let mut post_stmt = conn.prepare(
        "SELECT feed.title, post.title, post.guid, post.processed_audio_path, post.description, post.release_date, post.duration, post.image_url, post.chapter_data \
         FROM post JOIN feed ON feed.id = post.feed_id \
         WHERE post.feed_id = ?1 AND post.whitelisted = 1 AND post.processed_audio_path IS NOT NULL \
         ORDER BY post.release_date DESC, post.id DESC LIMIT ?2",
    )?;
    for feed_id in feed_ids {
        let rows = post_stmt.query_map((feed_id, args.limit_per_feed as i64), post_from_row)?;
        for row in rows {
            posts.push(row?);
        }
    }
    posts.sort_by(|a, b| b.release_date.cmp(&a.release_date));

    let last_changed_at: Option<String> = if args.require_auth {
        conn.query_row(
            "SELECT max(feed.last_changed_at) FROM feed JOIN feed_supporter ON feed_supporter.feed_id = feed.id WHERE feed_supporter.user_id = ?1",
            [args.user_id],
            |row| row.get(0),
        )?
    } else {
        conn.query_row("SELECT max(last_changed_at) FROM feed", [], |row| {
            row.get(0)
        })?
    };

    let link = append_feed_token(
        &format!(
            "{}/feed/user/{}",
            args.base_url.trim_end_matches('/'),
            args.user_id
        ),
        args.feed_token.as_deref(),
        args.feed_secret.as_deref(),
    );
    let (title, description) = aggregate_feed_metadata(&conn, args.require_auth, args.user_id)?;
    let image_url = format!(
        "{}/static/images/logos/manifest-icon-512.maskable.png",
        args.base_url.trim_end_matches('/')
    );
    let last_build_date = format_rfc2822(last_changed_at.as_deref());
    let xml = build_rss_xml(RssBuildArgs {
        title: &title,
        link: &link,
        description: &description,
        image_url: Some(&image_url),
        image_title: &title,
        last_build_date: &last_build_date,
        base_url: &args.base_url,
        feed_token: args.feed_token.as_deref(),
        feed_secret: args.feed_secret.as_deref(),
        posts: &posts,
        prepend_feed_title: true,
    });
    Ok(XmlResponse { xml })
}

fn aggregate_feed_metadata(
    conn: &Connection,
    require_auth: bool,
    user_id: i64,
) -> Result<(String, String)> {
    if require_auth {
        let username = conn
            .query_row(
                "SELECT username FROM users WHERE id = ?1",
                [user_id],
                |row| row.get::<_, String>(0),
            )
            .optional()?;
        if let Some(username) = username {
            return Ok((
                format!("Podly Podcasts - {username}"),
                format!(
                    "Aggregate feed for {username} - Last 3 processed episodes from each subscribed feed."
                ),
            ));
        }
    }

    Ok((
        "Podly Podcasts".to_string(),
        "Aggregate feed - Last 3 processed episodes from each subscribed feed.".to_string(),
    ))
}

#[derive(Serialize)]
struct FeedRefreshPlanResponse {
    updates: Value,
    new_posts: Vec<Value>,
    existing_post_updates: Vec<Value>,
}

fn plan_feed_refresh(args: FeedRefreshPlanArgs) -> Result<FeedRefreshPlanResponse> {
    let conn = open_readonly_sqlite(&args.db)?;
    let feed = query_refresh_feed(&conn, args.feed_id)?;
    let xml = fs::read_to_string(&args.feed_xml)
        .with_context(|| format!("failed to read feed XML {}", args.feed_xml.display()))?;
    let channel_image_url = parse_channel_image_url(&xml);
    let entries = parse_feed_entries(&xml, feed.image_url.as_deref())?;

    let mut updates = serde_json::Map::new();
    if let Some(new_image_url) = channel_image_url {
        if feed.image_url.as_deref() != Some(new_image_url.as_str()) {
            updates.insert("image_url".to_string(), json!(new_image_url));
        }
    }

    let posts = query_refresh_posts(&conn, feed.id)?;
    let oldest_release_date = posts
        .iter()
        .filter_map(|post| post.release_date.as_deref())
        .min()
        .map(str::to_string);
    let posts_by_guid: HashMap<&str, &FeedRefreshPostRow> = posts
        .iter()
        .map(|post| (post.guid.as_str(), post))
        .collect();
    let posts_by_url: HashMap<&str, &FeedRefreshPostRow> = posts
        .iter()
        .map(|post| (post.download_url.as_str(), post))
        .collect();

    let mut new_posts = Vec::new();
    let mut existing_post_updates = Vec::new();
    for entry in entries {
        let mut existing = posts_by_guid.get(entry.guid.as_str()).copied();
        let mut repaired_guid: Option<&str> = None;
        if existing.is_none() {
            existing = posts_by_url.get(entry.download_url.as_str()).copied();
            if let Some(post) = existing {
                if post.guid != entry.guid {
                    repaired_guid = Some(entry.guid.as_str());
                }
            }
        }

        if let Some(post) = existing {
            let update = existing_post_refresh_update(post, &entry, repaired_guid);
            if let Some(update) = update {
                existing_post_updates.push(update);
            }
        } else {
            let is_archive = match (
                oldest_release_date.as_deref(),
                entry.release_date.as_deref(),
            ) {
                (Some(oldest), Some(release)) => {
                    release_date_prefix(release) < release_date_prefix(oldest)
                }
                _ => false,
            };
            let whitelisted = args.auto_whitelist_new_posts && !is_archive;
            new_posts.push(json!({
                "guid": entry.guid,
                "title": entry.title,
                "description": entry.description,
                "download_url": entry.download_url,
                "release_date": entry.release_date,
                "duration": entry.duration,
                "image_url": entry.image_url,
                "whitelisted": whitelisted,
                "feed_id": feed.id,
            }));
        }
    }

    Ok(FeedRefreshPlanResponse {
        updates: Value::Object(updates),
        new_posts,
        existing_post_updates,
    })
}

fn query_refresh_feed(conn: &Connection, feed_id: i64) -> Result<FeedRefreshFeedRow> {
    conn.query_row(
        "SELECT id, image_url FROM feed WHERE id = ?1",
        [feed_id],
        |row| {
            Ok(FeedRefreshFeedRow {
                id: row.get(0)?,
                image_url: row.get(1)?,
            })
        },
    )
    .with_context(|| format!("feed {feed_id} not found"))
}

fn query_refresh_posts(conn: &Connection, feed_id: i64) -> Result<Vec<FeedRefreshPostRow>> {
    let mut stmt = conn.prepare(
        "SELECT id, guid, download_url, title, description, processed_audio_path,
                release_date, duration, image_url
           FROM post
          WHERE feed_id = ?1",
    )?;
    let rows = stmt.query_map([feed_id], |row| {
        Ok(FeedRefreshPostRow {
            id: row.get(0)?,
            guid: row.get(1)?,
            download_url: row.get(2)?,
            title: row.get(3)?,
            description: row.get(4)?,
            processed_audio_path: row.get(5)?,
            release_date: row.get(6)?,
            duration: get_duration_seconds(row, 7)?,
            image_url: row.get(8)?,
        })
    })?;
    Ok(rows.collect::<std::result::Result<Vec<_>, _>>()?)
}

fn existing_post_refresh_update(
    post: &FeedRefreshPostRow,
    entry: &ParsedFeedEntry,
    repaired_guid: Option<&str>,
) -> Option<Value> {
    let mut update = serde_json::Map::new();
    update.insert("post_id".to_string(), json!(post.id));
    if let Some(guid) = repaired_guid {
        update.insert("guid".to_string(), json!(guid));
    }
    if !entry.title.is_empty() && post.title != entry.title {
        update.insert("title".to_string(), json!(entry.title));
    }
    if post.description.as_deref().unwrap_or("") != entry.description {
        update.insert("description".to_string(), json!(entry.description));
    }
    if post.image_url != entry.image_url {
        update.insert("image_url".to_string(), json!(entry.image_url));
    }
    if post.processed_audio_path.is_none()
        && entry.duration.is_some()
        && post.duration != entry.duration
    {
        update.insert("duration".to_string(), json!(entry.duration));
    }
    if update.len() > 1 {
        Some(Value::Object(update))
    } else {
        None
    }
}

fn parse_feed_entries(xml: &str, feed_image_url: Option<&str>) -> Result<Vec<ParsedFeedEntry>> {
    let mut entries = Vec::new();
    for item in extract_xml_blocks(xml, "item") {
        let title = xml_text(&item, "title").unwrap_or_default();
        let download_url = find_feed_entry_audio_url(&item)
            .or_else(|| xml_text(&item, "guid"))
            .unwrap_or_default();
        let raw_guid = xml_text(&item, "guid").unwrap_or_default();
        let guid = if raw_guid.trim().is_empty() {
            uuid::Uuid::new_v5(&uuid::Uuid::NAMESPACE_URL, download_url.as_bytes()).to_string()
        } else {
            raw_guid.trim().to_string()
        };
        entries.push(ParsedFeedEntry {
            guid,
            title: title.trim().to_string(),
            description: parse_entry_description(&item),
            download_url,
            release_date: xml_text(&item, "pubDate").and_then(|value| parse_rfc2822_to_utc(&value)),
            duration: xml_text(&item, "duration")
                .or_else(|| xml_text(&item, "itunes:duration"))
                .and_then(|value| parse_duration_seconds_string(&value)),
            image_url: parse_entry_image_url(&item).or_else(|| feed_image_url.map(str::to_string)),
        });
    }
    Ok(entries)
}

fn parse_channel_image_url(xml: &str) -> Option<String> {
    let channel = extract_xml_blocks(xml, "channel").into_iter().next()?;
    if let Some(image_block) = extract_xml_blocks(&channel, "image").into_iter().next() {
        if let Some(url) = xml_text(&image_block, "url") {
            return Some(url);
        }
    }
    parse_itunes_image_href(&channel)
}

fn parse_entry_image_url(item: &str) -> Option<String> {
    parse_itunes_image_href(item)
        .or_else(|| parse_media_thumbnail_url(item))
        .or_else(|| {
            extract_xml_blocks(item, "image")
                .into_iter()
                .next()
                .and_then(|image| {
                    xml_text(&image, "url").or_else(|| Some(image.trim().to_string()))
                })
        })
}

fn parse_entry_description(item: &str) -> String {
    xml_text(item, "content:encoded")
        .or_else(|| xml_text(item, "description"))
        .or_else(|| xml_text(item, "summary"))
        .or_else(|| xml_text(item, "itunes:subtitle"))
        .unwrap_or_default()
        .trim()
        .to_string()
}

fn find_feed_entry_audio_url(item: &str) -> Option<String> {
    static ENCLOSURE_RE: OnceLock<Regex> = OnceLock::new();
    let re = ENCLOSURE_RE.get_or_init(|| Regex::new(r#"(?is)<enclosure\b([^>]*)>"#).unwrap());
    for caps in re.captures_iter(item) {
        let attrs = caps.get(1).map(|m| m.as_str()).unwrap_or("");
        let mime = xml_attr(attrs, "type").unwrap_or_default().to_lowercase();
        if is_audio_mime_type(&mime) {
            if let Some(url) = xml_attr(attrs, "url") {
                return Some(url);
            }
        }
    }
    static LINK_RE: OnceLock<Regex> = OnceLock::new();
    let link_re = LINK_RE.get_or_init(|| Regex::new(r#"(?is)<link\b([^>]*)>"#).unwrap());
    for caps in link_re.captures_iter(item) {
        let attrs = caps.get(1).map(|m| m.as_str()).unwrap_or("");
        let mime = xml_attr(attrs, "type").unwrap_or_default().to_lowercase();
        if is_audio_mime_type(&mime) {
            if let Some(url) = xml_attr(attrs, "href") {
                return Some(url);
            }
        }
    }
    None
}

fn is_audio_mime_type(mime: &str) -> bool {
    matches!(
        mime,
        "audio/mpeg"
            | "audio/mp3"
            | "audio/x-mp3"
            | "audio/mpeg3"
            | "audio/mp4"
            | "audio/m4a"
            | "audio/x-m4a"
            | "audio/aac"
            | "audio/wav"
            | "audio/x-wav"
            | "audio/ogg"
            | "audio/opus"
            | "audio/flac"
    )
}

// Cache compiled regexes keyed by tag/attr name. Without this, every call to
// xml_text / extract_xml_blocks / xml_attr recompiles a regex; for a feed with
// hundreds of items that means thousands of compilations and dominates wall
// time of the refresh planner.
fn block_regex(tag: &str) -> &'static Regex {
    static CACHE: OnceLock<Mutex<HashMap<String, &'static Regex>>> = OnceLock::new();
    let cache = CACHE.get_or_init(|| Mutex::new(HashMap::new()));
    let mut guard = cache.lock().unwrap();
    if let Some(re) = guard.get(tag) {
        return re;
    }
    let pattern = format!(
        r"(?is)<{tag}\b[^>]*>(.*?)</{tag}>",
        tag = regex::escape(tag)
    );
    let leaked: &'static Regex = Box::leak(Box::new(Regex::new(&pattern).unwrap()));
    guard.insert(tag.to_string(), leaked);
    leaked
}

fn attr_regex(name: &str) -> &'static Regex {
    static CACHE: OnceLock<Mutex<HashMap<String, &'static Regex>>> = OnceLock::new();
    let cache = CACHE.get_or_init(|| Mutex::new(HashMap::new()));
    let mut guard = cache.lock().unwrap();
    if let Some(re) = guard.get(name) {
        return re;
    }
    let pattern = format!(
        r#"(?is)\b{}\s*=\s*("([^"]*)"|'([^']*)')"#,
        regex::escape(name)
    );
    let leaked: &'static Regex = Box::leak(Box::new(Regex::new(&pattern).unwrap()));
    guard.insert(name.to_string(), leaked);
    leaked
}

fn extract_xml_blocks(xml: &str, tag: &str) -> Vec<String> {
    block_regex(tag)
        .captures_iter(xml)
        .filter_map(|caps| caps.get(1).map(|m| m.as_str().to_string()))
        .collect()
}

fn xml_text(xml: &str, tag: &str) -> Option<String> {
    let raw = block_regex(tag)
        .captures(xml)?
        .get(1)?
        .as_str()
        .trim()
        .to_string();
    Some(decode_xml_text(&strip_cdata(&raw)))
}

fn parse_itunes_image_href(xml: &str) -> Option<String> {
    static IMAGE_RE: OnceLock<Regex> = OnceLock::new();
    let re = IMAGE_RE.get_or_init(|| Regex::new(r#"(?is)<itunes:image\b([^>]*)>"#).unwrap());
    let attrs = re.captures(xml)?.get(1)?.as_str();
    xml_attr(attrs, "href")
}

fn parse_media_thumbnail_url(xml: &str) -> Option<String> {
    static THUMB_RE: OnceLock<Regex> = OnceLock::new();
    let re = THUMB_RE.get_or_init(|| Regex::new(r#"(?is)<media:thumbnail\b([^>]*)>"#).unwrap());
    let attrs = re.captures(xml)?.get(1)?.as_str();
    xml_attr(attrs, "url")
}

fn xml_attr(attrs: &str, name: &str) -> Option<String> {
    let caps = attr_regex(name).captures(attrs)?;
    let value = caps
        .get(2)
        .or_else(|| caps.get(3))
        .map(|m| m.as_str())
        .unwrap_or("");
    Some(decode_xml_text(value))
}

fn strip_cdata(value: &str) -> String {
    let trimmed = value.trim();
    if trimmed.starts_with("<![CDATA[") && trimmed.ends_with("]]>") {
        trimmed
            .trim_start_matches("<![CDATA[")
            .trim_end_matches("]]>")
            .to_string()
    } else {
        trimmed.to_string()
    }
}

fn decode_xml_text(value: &str) -> String {
    value
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", "\"")
        .replace("&apos;", "'")
}

fn parse_rfc2822_to_utc(value: &str) -> Option<String> {
    DateTime::parse_from_rfc2822(value)
        .ok()
        .map(|dt| dt.with_timezone(&Utc).to_rfc3339())
}

fn parse_duration_seconds_string(value: &str) -> Option<i64> {
    let trimmed = value.trim();
    if trimmed.is_empty() {
        return None;
    }
    if let Ok(seconds) = trimmed.parse::<i64>() {
        return (seconds >= 0).then_some(seconds);
    }
    let parts: Vec<&str> = trimmed.split(':').collect();
    if !(2..=3).contains(&parts.len()) || parts.iter().any(|part| part.trim().is_empty()) {
        return None;
    }
    let nums: Vec<i64> = parts
        .iter()
        .map(|part| part.trim().parse::<i64>())
        .collect::<std::result::Result<Vec<_>, _>>()
        .ok()?;
    match nums.as_slice() {
        [minutes, seconds] if *minutes >= 0 && *seconds >= 0 => Some(minutes * 60 + seconds),
        [hours, minutes, seconds] if *hours >= 0 && *minutes >= 0 && *seconds >= 0 => {
            Some(hours * 3600 + minutes * 60 + seconds)
        }
        _ => None,
    }
}

fn release_date_prefix(value: &str) -> &str {
    value.split('T').next().unwrap_or(value)
}

fn open_readonly_sqlite(path: &Path) -> Result<Connection> {
    let conn = Connection::open_with_flags(path, OpenFlags::SQLITE_OPEN_READ_ONLY)
        .with_context(|| "failed to open sqlite database read-only")?;
    conn.busy_timeout(std::time::Duration::from_secs(30))?;
    conn.pragma_update(None, "query_only", true)?;
    Ok(conn)
}

fn query_posts(conn: &Connection, sql: &str, feed_id: [i64; 1]) -> Result<Vec<PostRow>> {
    let mut stmt = conn.prepare(sql)?;
    let rows = stmt.query_map(feed_id, post_from_row)?;
    Ok(rows.collect::<std::result::Result<Vec<PostRow>, _>>()?)
}

fn post_from_row(row: &rusqlite::Row<'_>) -> rusqlite::Result<PostRow> {
    Ok(PostRow {
        feed_title: row.get(0)?,
        title: row.get(1)?,
        guid: row.get(2)?,
        processed_audio_path: row.get(3)?,
        description: row.get(4)?,
        release_date: row.get(5)?,
        duration: get_duration_seconds(row, 6)?,
        image_url: row.get(7)?,
        chapter_data: row.get(8)?,
    })
}

fn build_rss_xml(args: RssBuildArgs<'_>) -> String {
    let mut xml = String::from("<?xml version=\"1.0\" encoding=\"utf-8\"?>\n");
    xml.push_str("<rss version=\"2.0\" xmlns:itunes=\"http://www.itunes.com/dtds/podcast-1.0.dtd\" xmlns:content=\"http://purl.org/rss/1.0/modules/content/\">\n<channel>\n");
    push_text_element(&mut xml, "title", args.title);
    push_text_element(&mut xml, "link", args.link);
    push_text_element(
        &mut xml,
        "description",
        &normalize_feed_text(args.description),
    );
    push_text_element(&mut xml, "lastBuildDate", args.last_build_date);
    if let Some(image_url) = args.image_url {
        xml.push_str("<image>");
        push_text_element(&mut xml, "url", image_url);
        push_text_element(&mut xml, "title", args.image_title);
        push_text_element(&mut xml, "link", args.link);
        xml.push_str("</image>\n");
    }
    for post in args.posts {
        push_item(
            &mut xml,
            post,
            args.base_url,
            args.feed_token,
            args.feed_secret,
            args.prepend_feed_title,
        );
    }
    xml.push_str("</channel>\n</rss>\n");
    xml
}

fn push_item(
    xml: &mut String,
    post: &PostRow,
    base_url: &str,
    feed_token: Option<&str>,
    feed_secret: Option<&str>,
    prepend_feed_title: bool,
) {
    xml.push_str("<item>");
    let title = if prepend_feed_title {
        match &post.feed_title {
            Some(feed_title) => format!("[{feed_title}] {}", post.title),
            None => post.title.clone(),
        }
    } else {
        post.title.clone()
    };
    push_text_element(xml, "title", &title);
    let audio_url = append_feed_token(
        &format!(
            "{}/post/{}.mp3",
            base_url.trim_end_matches('/'),
            urlencoding_simple(&post.guid)
        ),
        feed_token,
        feed_secret,
    );
    let description = build_description(post);
    push_cdata_element(xml, "content:encoded", &description);
    push_cdata_element(xml, "description", &description);
    xml.push_str(&format!(
        "<enclosure url=\"{}\" type=\"audio/mpeg\" length=\"{}\"></enclosure>",
        xml_escape(&audio_url),
        enclosure_len(post)
    ));
    push_text_element(xml, "guid", &post.guid);
    if let Some(release_date) = post.release_date.as_deref() {
        push_text_element(xml, "pubDate", &format_rfc2822(Some(release_date)));
    }
    if let Some(image_url) = post.image_url.as_deref() {
        xml.push_str(&format!(
            "<itunes:image href=\"{}\"></itunes:image>",
            xml_escape(image_url)
        ));
    }
    if let Some(duration) = post.duration {
        if duration >= 0 {
            push_text_element(xml, "itunes:duration", &format_duration(duration));
        }
    }
    xml.push_str("</item>\n");
}

fn enclosure_len(post: &PostRow) -> u64 {
    post.processed_audio_path
        .as_deref()
        .and_then(|path| fs::metadata(path).ok())
        .map(|metadata| metadata.len())
        .unwrap_or(0)
}

fn build_description(post: &PostRow) -> String {
    let mut parts = Vec::new();
    if let Some(description) = post.description.as_deref() {
        if !description.is_empty() {
            parts.push(normalize_feed_text(description));
        }
    }
    let chapters = render_chapters(post.chapter_data.as_deref());
    if !chapters.is_empty() {
        parts.push(chapters);
    }
    parts.join("\n")
}

fn render_chapters(raw: Option<&str>) -> String {
    let Some(raw) = raw else {
        return String::new();
    };
    let Ok(Value::Object(data)) = serde_json::from_str::<Value>(raw) else {
        return String::new();
    };
    let chapters = data
        .get("chapters_for_output")
        .or_else(|| data.get("chapters_kept"));
    let Some(Value::Array(chapters)) = chapters else {
        return String::new();
    };
    let mut parsed = Vec::new();
    for chapter in chapters {
        let Value::Object(chapter) = chapter else {
            continue;
        };
        let title = chapter
            .get("title")
            .and_then(value_to_string)
            .unwrap_or_default()
            .trim()
            .to_string();
        if title.is_empty() {
            continue;
        }
        let start = match chapter.get("start_time") {
            Some(value) => match value_to_f64(value) {
                Some(start) => start,
                None => continue,
            },
            None => 0.0,
        }
        .max(0.0);
        parsed.push((start, title));
    }
    parsed.sort_by(|a, b| a.0.total_cmp(&b.0));
    if parsed.is_empty() {
        return String::new();
    }
    let items = parsed
        .into_iter()
        .map(|(start, title)| {
            format!(
                "<li>{} {}</li>",
                format_chapter_timestamp(start),
                html_escape(&title)
            )
        })
        .collect::<String>();
    format!("<p><strong>Podly Chapters</strong></p><ul>{items}</ul>")
}

fn append_feed_token(url: &str, token: Option<&str>, secret: Option<&str>) -> String {
    match (token, secret) {
        (Some(token), Some(secret)) if !token.is_empty() && !secret.is_empty() => {
            let separator = if url.contains('?') { '&' } else { '?' };
            format!(
                "{url}{separator}feed_token={}&feed_secret={}",
                urlencoding_simple(token),
                urlencoding_simple(secret)
            )
        }
        _ => url.to_string(),
    }
}

fn format_rfc2822(raw: Option<&str>) -> String {
    if let Some(raw) = raw {
        if let Ok(dt) = DateTime::parse_from_rfc3339(raw) {
            return dt.with_timezone(&Utc).to_rfc2822();
        }
        if let Ok(naive) = NaiveDateTime::parse_from_str(raw, "%Y-%m-%d %H:%M:%S%.f") {
            return DateTime::<Utc>::from_naive_utc_and_offset(naive, Utc).to_rfc2822();
        }
    }
    Utc::now().to_rfc2822()
}

fn format_duration(duration_seconds: i64) -> String {
    let total = duration_seconds.max(0);
    let hours = total / 3600;
    let minutes = (total % 3600) / 60;
    let seconds = total % 60;
    if hours > 0 {
        format!("{hours}:{minutes:02}:{seconds:02}")
    } else {
        format!("{minutes:02}:{seconds:02}")
    }
}

fn format_chapter_timestamp(seconds: f64) -> String {
    format_duration(seconds.max(0.0) as i64)
}

fn push_text_element(xml: &mut String, name: &str, value: &str) {
    xml.push_str(&format!("<{name}>{}</{name}>\n", xml_escape(value)));
}

fn push_cdata_element(xml: &mut String, name: &str, value: &str) {
    xml.push_str(&format!(
        "<{name}><![CDATA[{}]]></{name}>",
        value.replace("]]>", "]]]]><![CDATA[>")
    ));
}

fn normalize_feed_text(value: &str) -> String {
    value.replace('\u{00a0}', " ").replace(
        ['\u{200b}', '\u{200c}', '\u{200d}', '\u{2060}', '\u{feff}'],
        "",
    )
}

fn xml_escape(value: &str) -> String {
    value
        .replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
}

fn html_escape(value: &str) -> String {
    value
        .replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
        .replace('\'', "&#x27;")
}

fn urlencoding_simple(value: &str) -> String {
    value
        .bytes()
        .flat_map(|byte| match byte {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => {
                vec![byte as char]
            }
            _ => format!("%{byte:02X}").chars().collect(),
        })
        .collect()
}

fn value_to_i64(value: &Value) -> Option<i64> {
    match value {
        Value::Number(number) => number
            .as_i64()
            .or_else(|| number.as_u64().and_then(|value| i64::try_from(value).ok())),
        Value::String(text) => text.parse::<i64>().ok(),
        _ => None,
    }
}

fn value_to_f64(value: &Value) -> Option<f64> {
    match value {
        Value::Number(number) => number.as_f64(),
        Value::String(text) => text.parse::<f64>().ok(),
        _ => None,
    }
}

fn value_to_string(value: &Value) -> Option<String> {
    match value {
        Value::String(text) => Some(text.clone()),
        Value::Number(number) => Some(number.to_string()),
        Value::Bool(value) => Some(value.to_string()),
        _ => None,
    }
}

fn print_feed(response: &XmlResponse, raw_xml: bool) -> Result<()> {
    let mut output = BufWriter::new(std::io::stdout().lock());
    write_feed(&mut output, response, raw_xml)
}

fn write_feed(output: &mut impl Write, response: &XmlResponse, raw_xml: bool) -> Result<()> {
    if raw_xml {
        output.write_all(response.xml.as_bytes())?;
        output.flush()?;
        Ok(())
    } else {
        write_json(output, response)
    }
}

fn print_json<T: Serialize>(value: &T) -> Result<()> {
    // Stream through a bounded buffer instead of allocating another copy of
    // the complete response. Lock stdout once and propagate write/flush errors.
    let mut output = BufWriter::new(std::io::stdout().lock());
    write_json(&mut output, value)
}

fn write_json<T: Serialize>(output: &mut impl Write, value: &T) -> Result<()> {
    serde_json::to_writer(&mut *output, value)?;
    output.write_all(b"\n")?;
    output.flush()?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use rusqlite::params;

    #[test]
    fn feed_output_preserves_raw_xml_and_legacy_json() {
        let response = XmlResponse {
            xml: "<?xml version=\"1.0\" encoding=\"utf-8\"?>\n<rss><title>Café &amp; tea</title></rss>\n".into(),
        };
        let mut raw = Vec::new();
        write_feed(&mut raw, &response, true).unwrap();
        assert_eq!(raw, response.xml.as_bytes());
        let mut legacy = Vec::new();
        write_feed(&mut legacy, &response, false).unwrap();
        let decoded: Value = serde_json::from_slice(&legacy).unwrap();
        assert_eq!(decoded["xml"], response.xml);
    }

    #[test]
    fn feed_raw_xml_flag_parses_for_both_renderers() {
        for renderer in ["render", "render-aggregate"] {
            let mut args = vec![
                "podly_tools",
                "feed",
                renderer,
                "--db",
                "db.sqlite",
                "--base-url",
                "https://podly.test",
                "--raw-xml",
            ];
            if renderer == "render" {
                args.extend(["--feed-id", "1"]);
            } else {
                args.extend(["--user-id", "1", "--limit-per-feed", "3"]);
            }
            let cli = Cli::try_parse_from(args).unwrap();
            let Commands::Feed(feed) = cli.command else {
                panic!("expected feed command")
            };
            assert!(feed.raw_xml);
        }
    }

    #[test]
    fn streamed_json_preserves_wire_format() {
        let payload = json!({
            "items": [{"title": "Café \"quoted\"\n", "duration": 1800.5}],
            "raw": serde_json::value::RawValue::from_string("{\"count\":1}".into()).unwrap(),
        });
        let mut output = Vec::new();
        write_json(&mut output, &payload).unwrap();
        let mut expected = serde_json::to_vec(&payload).unwrap();
        expected.push(b'\n');
        assert_eq!(output, expected);
    }

    #[test]
    fn streamed_json_propagates_output_errors() {
        struct FailingOutput {
            fail_write: bool,
        }
        impl Write for FailingOutput {
            fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
                if self.fail_write {
                    Err(std::io::Error::other("write failed"))
                } else {
                    Ok(bytes.len())
                }
            }
            fn flush(&mut self) -> std::io::Result<()> {
                Err(std::io::Error::other("flush failed"))
            }
        }
        for fail_write in [true, false] {
            let error =
                write_json(&mut FailingOutput { fail_write }, &json!({"ok": true})).unwrap_err();
            assert!(error.to_string().contains(if fail_write {
                "write failed"
            } else {
                "flush failed"
            }));
        }
    }

    #[test]
    fn merge_windows_sorts_and_coalesces_overlaps() {
        assert_eq!(
            merge_windows(vec![(300, 400), (100, 200), (180, 250), (500, 550)]),
            vec![(100, 250), (300, 400), (500, 550)]
        );
    }

    #[test]
    fn keep_segments_inverts_removed_windows() {
        assert_eq!(
            keep_segments(&[(100, 200), (300, 350)], 500),
            vec![(0, 100), (200, 300), (350, 500)]
        );
    }

    #[test]
    fn duration_and_chapter_timestamp_format_match_python_shape() {
        assert_eq!(format_duration(65), "01:05");
        assert_eq!(format_duration(3_665), "1:01:05");
        assert_eq!(format_chapter_timestamp(125.9), "02:05");
    }

    #[test]
    fn feed_token_url_appending_encodes_values() {
        assert_eq!(
            append_feed_token("https://podly.test/feed/1", Some("tok en"), Some("s&c")),
            "https://podly.test/feed/1?feed_token=tok%20en&feed_secret=s%26c"
        );
        assert_eq!(
            append_feed_token("https://podly.test/feed/1?x=1", Some("tok"), Some("sec")),
            "https://podly.test/feed/1?x=1&feed_token=tok&feed_secret=sec"
        );
    }

    #[test]
    fn render_chapters_prefers_output_chapters_and_escapes_titles() {
        let raw = r#"{
            "chapters_kept": [{"title": "ignored", "start_time": 0}],
            "chapters_for_output": [
                {"title": "B < C", "start_time": 65},
                {"title": "Intro", "start_time": 0},
                {"title": "\"Quoted\"", "start_time": 130},
                {"title": "skip invalid", "start_time": {"bad": true}}
            ]
        }"#;

        assert_eq!(
            render_chapters(Some(raw)),
            "<p><strong>Podly Chapters</strong></p><ul><li>00:00 Intro</li><li>01:05 B &lt; C</li><li>02:10 &quot;Quoted&quot;</li></ul>"
        );
    }

    #[test]
    fn normalize_word_timestamps_stream_contract_matches_python_filtering() {
        let dir = tempfile::tempdir().unwrap();
        let input = dir.path().join("input.json");
        let output = dir.path().join("output.json");
        fs::write(
            &input,
            r#"[
                {"sequence_num": "2", "words": [
                    {"word": "hello", "start": "1.0", "end": "1.5", "score": "0.9"},
                    {"word": "drop", "start": 2.0, "end": 1.0}
                ]},
                {"sequence_num": 3, "words": []}
            ]"#,
        )
        .unwrap();

        normalize_word_timestamps(TranscriptNormalizeArgs {
            input,
            output: output.clone(),
        })
        .unwrap();

        let value: Value = serde_json::from_str(&fs::read_to_string(output).unwrap()).unwrap();
        assert_eq!(
            value,
            serde_json::json!([
                {"sequence_num": 2, "words": [
                    {"word": "hello", "start": 1.0, "end": 1.5, "score": 0.9}
                ]}
            ])
        );
    }

    #[test]
    fn feed_render_reads_sqlite_and_includes_expected_rss_bits() {
        let dir = tempfile::tempdir().unwrap();
        let db_path = dir.path().join("podly.sqlite");
        let audio_path = dir.path().join("episode.mp3");
        fs::write(&audio_path, b"audio-bytes").unwrap();
        let conn = Connection::open(&db_path).unwrap();
        conn.execute_batch(
            "CREATE TABLE feed (
                id INTEGER PRIMARY KEY,
                title TEXT NOT NULL,
                description TEXT,
                image_url TEXT,
                last_changed_at TEXT
            );
            CREATE TABLE post (
                id INTEGER PRIMARY KEY,
                feed_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                guid TEXT NOT NULL,
                processed_audio_path TEXT,
                description TEXT,
                release_date TEXT,
                duration INTEGER,
                image_url TEXT,
                chapter_data TEXT,
                whitelisted BOOLEAN NOT NULL DEFAULT 0
            );",
        )
        .unwrap();
        conn.execute(
            "INSERT INTO feed (id, title, description, image_url, last_changed_at)
             VALUES (1, 'Feed & One', 'Desc', 'https://img.test/feed.png', '2024-01-02 03:04:05')",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO post (feed_id, title, guid, processed_audio_path, description, release_date, duration, image_url, chapter_data, whitelisted)
             VALUES (1, 'Episode <One>', 'guid-1', ?1, '<p>Hello</p>', '2024-01-03 04:05:06', 65, 'https://img.test/ep.png', ?2, 1)",
            params![
                audio_path.to_string_lossy(),
                r#"{"chapters_for_output":[{"title":"Intro","start_time":0}]}"#
            ],
        )
        .unwrap();
        drop(conn);

        let xml = render_feed(FeedRenderArgs {
            db: db_path,
            feed_id: 1,
            base_url: "https://podly.test".to_string(),
            include_unprocessed: false,
            feed_token: Some("tok".to_string()),
            feed_secret: Some("sec".to_string()),
        })
        .unwrap()
        .xml;

        assert!(xml.contains("<title>[podly] Feed &amp; One</title>"));
        assert!(
            xml.contains("https://podly.test/post/guid-1.mp3?feed_token=tok&amp;feed_secret=sec")
        );
        assert!(xml.contains("<itunes:duration>01:05</itunes:duration>"));
        assert!(xml.contains("<p><strong>Podly Chapters</strong></p><ul><li>00:00 Intro</li></ul>"));
        assert!(xml.contains("length=\"11\""));
    }

    #[test]
    fn feed_render_hides_posts_with_active_processing_job() {
        // include_unprocessed=true must still hide posts whose first
        // processing pass is queued or running, so podcast clients don't
        // cache an un-chaptered MP3.
        let dir = tempfile::tempdir().unwrap();
        let db_path = dir.path().join("podly.sqlite");
        let conn = Connection::open(&db_path).unwrap();
        conn.execute_batch(
            "CREATE TABLE feed (
                id INTEGER PRIMARY KEY,
                title TEXT NOT NULL,
                description TEXT,
                image_url TEXT,
                last_changed_at TEXT
            );
            CREATE TABLE post (
                id INTEGER PRIMARY KEY,
                feed_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                guid TEXT NOT NULL,
                processed_audio_path TEXT,
                description TEXT,
                release_date TEXT,
                duration INTEGER,
                image_url TEXT,
                chapter_data TEXT,
                whitelisted BOOLEAN NOT NULL DEFAULT 0
            );
            CREATE TABLE processing_job (
                id TEXT PRIMARY KEY,
                post_guid TEXT NOT NULL,
                status TEXT NOT NULL
            );",
        )
        .unwrap();
        conn.execute(
            "INSERT INTO feed (id, title, last_changed_at)
             VALUES (1, 'Feed', '2024-01-01 00:00:00')",
            [],
        )
        .unwrap();
        // historical: no processed audio, no job → visible
        conn.execute(
            "INSERT INTO post (feed_id, title, guid, release_date)
             VALUES (1, 'Historical', 'g-historical', '2023-12-01 00:00:00')",
            [],
        )
        .unwrap();
        // new, first-time processing in flight → hidden
        conn.execute(
            "INSERT INTO post (feed_id, title, guid, release_date)
             VALUES (1, 'Brand New', 'g-new', '2024-01-04 00:00:00')",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO processing_job (id, post_guid, status)
             VALUES ('job-new', 'g-new', 'running')",
            [],
        )
        .unwrap();
        // processed already, currently re-processing → visible (client cached
        // metadata long ago; hiding now would just make it disappear briefly)
        conn.execute(
            "INSERT INTO post (feed_id, title, guid, processed_audio_path, release_date)
             VALUES (1, 'Re-processing', 'g-reprocess', '/srv/r.mp3', '2024-01-03 00:00:00')",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO processing_job (id, post_guid, status)
             VALUES ('job-rep', 'g-reprocess', 'pending')",
            [],
        )
        .unwrap();
        // permanently failed → visible (no terminal-status job blocks it)
        conn.execute(
            "INSERT INTO post (feed_id, title, guid, release_date)
             VALUES (1, 'Failed', 'g-failed', '2024-01-02 00:00:00')",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO processing_job (id, post_guid, status)
             VALUES ('job-failed', 'g-failed', 'failed')",
            [],
        )
        .unwrap();
        // queued (pending, not yet started) first-time processing → hidden,
        // same as the 'running' case.
        conn.execute(
            "INSERT INTO post (feed_id, title, guid, release_date)
             VALUES (1, 'Queued Waiting', 'g-queued', '2024-01-05 00:00:00')",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO processing_job (id, post_guid, status)
             VALUES ('job-queued', 'g-queued', 'pending')",
            [],
        )
        .unwrap();
        drop(conn);

        let xml = render_feed(FeedRenderArgs {
            db: db_path,
            feed_id: 1,
            base_url: "https://podly.test".to_string(),
            include_unprocessed: true,
            feed_token: None,
            feed_secret: None,
        })
        .unwrap()
        .xml;

        assert!(xml.contains("g-historical"));
        assert!(xml.contains("g-reprocess"));
        assert!(xml.contains("g-failed"));
        assert!(!xml.contains("g-new"));
        assert!(!xml.contains("g-queued"));
    }

    fn seed_posts_listing_db(db_path: &Path) {
        let conn = Connection::open(db_path).unwrap();
        conn.execute_batch(
            "CREATE TABLE feed (
                id INTEGER PRIMARY KEY,
                title TEXT NOT NULL
            );
            CREATE TABLE post (
                id INTEGER PRIMARY KEY,
                feed_id INTEGER NOT NULL,
                guid TEXT NOT NULL,
                download_url TEXT NOT NULL,
                title TEXT NOT NULL,
                unprocessed_audio_path TEXT,
                processed_audio_path TEXT,
                description TEXT,
                release_date TEXT,
                duration INTEGER,
                whitelisted BOOLEAN NOT NULL DEFAULT 0,
                image_url TEXT,
                download_count INTEGER,
                chapter_data TEXT,
                transcript_word_timestamps TEXT,
                bleep_windows TEXT,
                refined_ad_boundaries TEXT
            );",
        )
        .unwrap();
        conn.execute("INSERT INTO feed (id, title) VALUES (1, 'Feed')", [])
            .unwrap();
        // 3 posts: two whitelisted with newer release_dates, one not whitelisted older.
        // Heavy JSON columns are stuffed with dummy payloads to confirm the
        // implementation never touches them.
        let heavy = "x".repeat(200_000);
        conn.execute(
            "INSERT INTO post (feed_id, guid, download_url, title, description, release_date,
                duration, whitelisted, processed_audio_path, image_url, download_count,
                chapter_data, transcript_word_timestamps)
             VALUES (1, 'g-3', 'https://e.test/3.mp3', 'Episode 3', '<p>three</p>',
	                '2024-03-03 00:00:00', 1800.5, 1, '/srv/3.mp3', 'https://i/3.png', 12,
	                ?1, ?2)",
            params![
                r#"{"chapters_for_output":[{"title":"Hello","start_time":0}]}"#,
                heavy
            ],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO post (feed_id, guid, download_url, title, description, release_date,
                duration, whitelisted, image_url, download_count)
             VALUES (1, 'g-2', 'https://e.test/2.mp3', 'Episode 2', '<p>two</p>',
                '2024-02-02 00:00:00', 1700, 1, 'https://i/2.png', 5)",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO post (feed_id, guid, download_url, title, release_date, whitelisted)
             VALUES (1, 'g-1', 'https://e.test/1.mp3', 'Episode 1', '2024-01-01 00:00:00', 0)",
            [],
        )
        .unwrap();
    }

    #[test]
    fn posts_feed_list_returns_paged_envelope_matching_python_shape() {
        let dir = tempfile::tempdir().unwrap();
        let db_path = dir.path().join("podly.sqlite");
        seed_posts_listing_db(&db_path);

        let payload = render_feed_posts(PostsFeedListArgs {
            db: db_path,
            feed_id: 1,
            page: 1,
            page_size: 25,
            whitelisted_only: false,
        })
        .unwrap();

        assert_eq!(payload["total"], 3);
        assert_eq!(payload["whitelisted_total"], 2);
        assert_eq!(payload["page"], 1);
        assert_eq!(payload["page_size"], 25);
        assert_eq!(payload["total_pages"], 1);

        let items = payload["items"].as_array().unwrap();
        assert_eq!(items.len(), 3);
        // Newest first by release_date desc, then id desc — g-3 → g-2 → g-1
        assert_eq!(items[0]["guid"], "g-3");
        assert_eq!(items[1]["guid"], "g-2");
        assert_eq!(items[2]["guid"], "g-1");

        let first = &items[0];
        assert_eq!(first["title"], "Episode 3");
        assert_eq!(first["whitelisted"], true);
        assert_eq!(first["has_processed_audio"], true);
        assert_eq!(first["has_unprocessed_audio"], false);
        assert_eq!(first["download_count"], 12);
        assert_eq!(first["duration"], json!(1800.5));
        assert_eq!(items[1]["duration"], json!(1700));
        assert_eq!(first["release_date"], "2024-03-03T00:00:00");
        assert!(first["podly_description_html"]
            .as_str()
            .unwrap()
            .contains("Podly Chapters"));
        // The heavy transcript_word_timestamps column must not appear in
        // the response at all.
        let serialized = serde_json::to_string(&payload).unwrap();
        assert!(!serialized.contains("transcript_word_timestamps"));
        assert!(!serialized.contains("xxxxxxxxxx"));
    }

    #[test]
    fn posts_feed_list_respects_pagination_and_whitelist_filter() {
        let dir = tempfile::tempdir().unwrap();
        let db_path = dir.path().join("podly.sqlite");
        seed_posts_listing_db(&db_path);

        let page_one = render_feed_posts(PostsFeedListArgs {
            db: db_path.clone(),
            feed_id: 1,
            page: 1,
            page_size: 2,
            whitelisted_only: false,
        })
        .unwrap();
        assert_eq!(page_one["total"], 3);
        assert_eq!(page_one["total_pages"], 2);
        assert_eq!(page_one["items"].as_array().unwrap().len(), 2);

        let page_two = render_feed_posts(PostsFeedListArgs {
            db: db_path.clone(),
            feed_id: 1,
            page: 2,
            page_size: 2,
            whitelisted_only: false,
        })
        .unwrap();
        assert_eq!(page_two["items"].as_array().unwrap().len(), 1);
        assert_eq!(page_two["items"][0]["guid"], "g-1");

        let whitelisted = render_feed_posts(PostsFeedListArgs {
            db: db_path,
            feed_id: 1,
            page: 1,
            page_size: 25,
            whitelisted_only: true,
        })
        .unwrap();
        assert_eq!(whitelisted["total"], 2);
        assert!(whitelisted["items"]
            .as_array()
            .unwrap()
            .iter()
            .all(|item| item["whitelisted"] == true));
    }

    #[test]
    fn posts_feed_list_signals_missing_feed() {
        let dir = tempfile::tempdir().unwrap();
        let db_path = dir.path().join("podly.sqlite");
        seed_posts_listing_db(&db_path);

        let payload = render_feed_posts(PostsFeedListArgs {
            db: db_path,
            feed_id: 999,
            page: 1,
            page_size: 25,
            whitelisted_only: false,
        })
        .unwrap();
        assert_eq!(payload["not_found"], true);
    }

    #[test]
    fn posts_feed_list_handles_empty_and_out_of_range_pages() {
        let dir = tempfile::tempdir().unwrap();
        let db_path = dir.path().join("podly.sqlite");
        seed_posts_listing_db(&db_path);
        let conn = Connection::open(&db_path).unwrap();
        conn.execute("INSERT INTO feed (id, title) VALUES (2, 'Empty')", [])
            .unwrap();
        for whitelisted_only in [false, true] {
            for feed_id in [1, 2] {
                let payload = render_feed_posts(PostsFeedListArgs {
                    db: db_path.clone(),
                    feed_id,
                    page: 100,
                    page_size: 2,
                    whitelisted_only,
                })
                .unwrap();
                let total = if feed_id == 2 {
                    0
                } else if whitelisted_only {
                    2
                } else {
                    3
                };
                assert_eq!(payload["items"], json!([]));
                assert_eq!(payload["total"], total);
                assert_eq!(
                    payload["whitelisted_total"],
                    if feed_id == 2 { 0 } else { 2 }
                );
                assert_eq!(payload["total_pages"], (total + 1) / 2);
            }
        }
    }

    #[test]
    fn posts_feed_list_propagates_row_decode_errors() {
        let dir = tempfile::tempdir().unwrap();
        let db_path = dir.path().join("podly.sqlite");
        seed_posts_listing_db(&db_path);
        let conn = Connection::open(&db_path).unwrap();
        // SQLite permits text in INTEGER columns. Do not silently omit a bad
        // row while consuming the cursor; the wrapper must get an error.
        conn.execute(
            "UPDATE post SET download_count = 'invalid' WHERE guid = 'g-2'",
            [],
        )
        .unwrap();
        assert!(render_feed_posts(PostsFeedListArgs {
            db: db_path,
            feed_id: 1,
            page: 1,
            page_size: 25,
            whitelisted_only: false,
        })
        .is_err());
    }

    #[test]
    fn chapters_write_replaces_chap_and_ctoc_frames() {
        let dir = tempfile::tempdir().unwrap();
        let audio = dir.path().join("audio.mp3");
        let chapters_json = dir.path().join("chapters.json");
        let removed_json = dir.path().join("removed.json");
        fs::write(&audio, b"not-real-audio-but-id3-can-write-tags").unwrap();
        fs::write(
            &chapters_json,
            r#"[
                {"title":"Intro","start_time_ms":0,"end_time_ms":1000},
                {"title":"Next","start_time_ms":1000,"end_time_ms":2000}
            ]"#,
        )
        .unwrap();
        fs::write(&removed_json, "[]").unwrap();

        write_chapters(ChaptersWriteArgs {
            audio: audio.clone(),
            chapters_json,
            removed_windows_json: removed_json,
        })
        .unwrap();

        let tag = Tag::read_from_path(audio).unwrap();
        assert_eq!(tag.chapters().count(), 2);
        assert_eq!(tag.tables_of_contents().count(), 1);
    }

    #[test]
    fn chapters_read_returns_empty_without_chap_frames() {
        let dir = tempfile::tempdir().unwrap();
        let audio = dir.path().join("audio.mp3");
        fs::write(&audio, b"not-real-audio").unwrap();

        let response = read_chapters(ChaptersReadArgs { audio }).unwrap();

        assert!(response.chapters.is_empty());
    }

    #[test]
    fn chapters_read_reads_file_written_by_chapters_write() {
        let dir = tempfile::tempdir().unwrap();
        let audio = dir.path().join("audio.mp3");
        let chapters_json = dir.path().join("chapters.json");
        let removed_json = dir.path().join("removed.json");
        fs::write(&audio, b"not-real-audio-but-id3-can-write-tags").unwrap();
        fs::write(
            &chapters_json,
            r#"[
                {"title":"Sponsor","start_time_ms":1000,"end_time_ms":2000},
                {"title":"Intro","start_time_ms":0,"end_time_ms":1000}
            ]"#,
        )
        .unwrap();
        fs::write(&removed_json, "[]").unwrap();

        write_chapters(ChaptersWriteArgs {
            audio: audio.clone(),
            chapters_json,
            removed_windows_json: removed_json,
        })
        .unwrap();

        let response = read_chapters(ChaptersReadArgs { audio }).unwrap();

        assert_eq!(response.chapters.len(), 2);
        assert_eq!(response.chapters[0].element_id, "chp0");
        assert_eq!(response.chapters[0].title, "Intro");
        assert_eq!(response.chapters[0].start_time_ms, 0);
        assert_eq!(response.chapters[1].title, "Sponsor");
    }

    #[test]
    fn chapters_detect_filters_titles_case_insensitively() {
        let chapters = vec![
            ChapterResponseItem {
                element_id: "chp0".to_string(),
                title: "Intro".to_string(),
                start_time_ms: 0,
                end_time_ms: 1000,
            },
            ChapterResponseItem {
                element_id: "chp1".to_string(),
                title: "Sponsored Break".to_string(),
                start_time_ms: 1000,
                end_time_ms: 2500,
            },
        ];

        let filters = parse_filter_strings_csv(" sponsor, advertisement ,,");
        let (keep, remove) = filter_chapters_by_strings(chapters, &filters);

        assert_eq!(keep.len(), 1);
        assert_eq!(keep[0].title, "Intro");
        assert_eq!(remove.len(), 1);
        assert_eq!(remove[0].title, "Sponsored Break");
    }

    #[test]
    fn stats_render_reads_seeded_sqlite_and_shapes_response() {
        let dir = tempfile::tempdir().unwrap();
        let db_path = dir.path().join("podly.sqlite");
        let log_path = dir.path().join("app.log");
        fs::write(
            &log_path,
            "2026-05-08 12:00:00,000 INFO [job_status] post_guid=slash/guid job_id=job-1 step_name=Processing audio\n",
        )
        .unwrap();
        let conn = Connection::open(&db_path).unwrap();
        conn.execute_batch(
            "CREATE TABLE feed (
                id INTEGER PRIMARY KEY, title TEXT NOT NULL, ad_detection_strategy TEXT NOT NULL,
                chapter_filter_strings TEXT
            );
            CREATE TABLE post (
                id INTEGER PRIMARY KEY, feed_id INTEGER NOT NULL, guid TEXT NOT NULL,
                title TEXT NOT NULL, download_url TEXT NOT NULL, unprocessed_audio_path TEXT,
                processed_audio_path TEXT, release_date TEXT, duration REAL, whitelisted INTEGER NOT NULL,
                download_count INTEGER, chapter_data TEXT, bleep_windows TEXT, refined_ad_boundaries TEXT
            );
            CREATE TABLE model_call (
                id INTEGER PRIMARY KEY, post_id INTEGER NOT NULL, first_segment_sequence_num INTEGER NOT NULL,
                last_segment_sequence_num INTEGER NOT NULL, model_name TEXT NOT NULL, prompt TEXT NOT NULL,
                response TEXT, timestamp TEXT, status TEXT NOT NULL, error_message TEXT, retry_attempts INTEGER,
                next_retry_at TEXT, service_tier TEXT, prompt_tokens INTEGER, cached_prompt_tokens INTEGER,
                completion_tokens INTEGER, total_tokens INTEGER
            );
            CREATE TABLE transcript_segment (
                id INTEGER PRIMARY KEY, post_id INTEGER NOT NULL, sequence_num INTEGER NOT NULL,
                start_time REAL NOT NULL, end_time REAL NOT NULL, text TEXT NOT NULL, speaker_label TEXT
            );
            CREATE TABLE audio_segment (
                id INTEGER PRIMARY KEY, post_id INTEGER NOT NULL, model_call_id INTEGER,
                label TEXT NOT NULL, start_time REAL NOT NULL, end_time REAL NOT NULL
            );
            CREATE TABLE identification (
                id INTEGER PRIMARY KEY, transcript_segment_id INTEGER NOT NULL, model_call_id INTEGER NOT NULL,
                confidence REAL, label TEXT NOT NULL
            );
            CREATE TABLE processing_job (
                id TEXT PRIMARY KEY, post_guid TEXT NOT NULL, created_at TEXT
            );",
        )
        .unwrap();
        conn.execute("INSERT INTO feed VALUES (1, 'Feed', 'llm', NULL)", [])
            .unwrap();
        conn.execute(
            "INSERT INTO post VALUES (
                1, 1, 'slash/guid', 'Episode', 'https://example.com/audio.mp3',
                '/tmp/in.mp3', '/tmp/out.mp3', '2026-05-08 12:00:00.000000',
                90.0, 1, 2, NULL,
                '[{\"start_time\":5.0,\"end_time\":6.0}]',
                '[{\"orig_start\":10.0,\"orig_end\":20.0,\"refined_start\":9.5,\"refined_end\":20.5}]'
            )",
            [],
        )
        .unwrap();
        // Order by (model_name, first_segment_sequence_num); use 'model-b'
        // for the chapter row so it sorts after [0] and doesn't break the
        // existing assertion that [0] is the primary classifier row.
        conn.execute(
            "INSERT INTO model_call VALUES (1, 1, 0, 1, 'model-a', 'prompt', 'response', '2026-05-08 12:00:01.000000', 'success', NULL, 2, NULL, 'flex', 100, 25, 50, 150),
                                            (2, 1, -100, -100, 'model-b', 'chap prompt', 'chap response', '2026-05-08 12:00:02.000000', 'success', NULL, 1, NULL, NULL, NULL, NULL, NULL, NULL)",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO transcript_segment VALUES
                (1, 1, 0, 0.0, 8.0, 'content', 'A'),
                (2, 1, 1, 10.0, 20.0, 'visit example.com code SAVE now', 'B')",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO audio_segment VALUES (1, 1, NULL, 'music', 20.0, 21.0)",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO identification VALUES (1, 2, 1, 0.95, 'ad')",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO processing_job VALUES ('job-1', 'slash/guid', '2026-05-08 12:00:02')",
            [],
        )
        .unwrap();
        drop(conn);

        let payload = render_stats(StatsRenderArgs {
            db: db_path,
            post_guid: "slash/guid".to_string(),
            min_confidence: 0.8,
            min_ad_segment_separation_seconds: 60.0,
            enable_boundary_refinement: true,
            stats_debug: false,
            log_path,
            in_root: dir.path().join("in"),
            srv_root: dir.path().join("srv"),
        })
        .unwrap();
        let stats = payload.get("stats").unwrap();

        assert_eq!(stats["post"]["guid"], "slash/guid");
        assert_eq!(stats["post"]["release_date"], "2026-05-08T12:00:00");
        assert_eq!(stats["processing_stats"]["total_segments"], 2);
        assert_eq!(stats["processing_stats"]["ad_segments_count"], 1);
        assert_eq!(stats["model_calls"][0]["retry_count"], 1);
        // service_tier surfaces on the row that was made with Flex so the
        // debug UI's Tier column can render the chip. The second row used
        // the default tier and is rendered as the sentinel chapter label.
        assert_eq!(stats["model_calls"][0]["service_tier"], "flex");
        assert_eq!(stats["model_calls"][0]["segment_range"], "0-1");
        // Token usage is surfaced alongside service_tier so the debug modal
        // can render "tokens (flex)" without an extra round-trip.
        assert_eq!(stats["model_calls"][0]["prompt_tokens"], 100);
        assert_eq!(stats["model_calls"][0]["cached_prompt_tokens"], 25);
        assert_eq!(stats["model_calls"][0]["completion_tokens"], 50);
        assert_eq!(stats["model_calls"][0]["total_tokens"], 150);
        assert_eq!(stats["model_calls"][1]["service_tier"], Value::Null);
        assert_eq!(stats["model_calls"][1]["cached_prompt_tokens"], Value::Null);
        assert_eq!(stats["model_calls"][1]["total_tokens"], Value::Null);
        assert_eq!(
            stats["model_calls"][1]["segment_range"],
            "chapter titles (LLM)"
        );
        assert_eq!(stats["related_logs"]["latest_job_id"], "job-1");
        assert_eq!(
            stats["related_logs"]["entries"].as_array().unwrap().len(),
            1
        );
    }

    #[test]
    fn feed_refresh_plan_shapes_new_existing_archive_and_guid_repair() {
        let dir = tempfile::tempdir().unwrap();
        let db_path = dir.path().join("podly.sqlite");
        let xml_path = dir.path().join("feed.xml");
        fs::write(
            &xml_path,
            r#"<?xml version="1.0" encoding="utf-8"?>
            <rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" xmlns:content="http://purl.org/rss/1.0/modules/content/">
              <channel>
                <title>Feed</title>
                <image><url>https://img.test/new-feed.png</url></image>
                <item>
                  <title>New Episode</title>
                  <guid>new-guid</guid>
                  <pubDate>Fri, 08 May 2026 12:00:00 +0000</pubDate>
                  <content:encoded><![CDATA[<p>New desc</p>]]></content:encoded>
                  <itunes:duration>01:02:03</itunes:duration>
                  <itunes:image href="https://img.test/new-ep.png" />
                  <enclosure url="https://cdn.test/new.mp3" type="audio/mpeg" />
                </item>
                <item>
                  <title>Existing Updated</title>
                  <guid>existing-guid</guid>
                  <description>Changed desc</description>
                  <itunes:duration>321</itunes:duration>
                  <enclosure url="https://cdn.test/existing.mp3" type="audio/mpeg" />
                </item>
                <item>
                  <title>Repaired Guid</title>
                  <guid>corrected-guid</guid>
                  <description>Legacy desc</description>
                  <enclosure url="https://cdn.test/legacy.mp3" type="audio/mpeg" />
                </item>
                <item>
                  <title>Archive Episode</title>
                  <guid>archive-guid</guid>
                  <pubDate>Fri, 01 May 2020 12:00:00 +0000</pubDate>
                  <description>Old desc</description>
                  <enclosure url="https://cdn.test/archive.mp3" type="audio/mpeg" />
                </item>
              </channel>
            </rss>"#,
        )
        .unwrap();
        let conn = Connection::open(&db_path).unwrap();
        conn.execute_batch(
            "CREATE TABLE feed (
                id INTEGER PRIMARY KEY,
                image_url TEXT
            );
            CREATE TABLE post (
                id INTEGER PRIMARY KEY,
                feed_id INTEGER NOT NULL,
                guid TEXT NOT NULL,
                download_url TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT,
                processed_audio_path TEXT,
                release_date TEXT,
                duration INTEGER,
                image_url TEXT
            );",
        )
        .unwrap();
        conn.execute(
            "INSERT INTO feed VALUES (1, 'https://img.test/old-feed.png')",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO post VALUES
                (1, 1, 'existing-guid', 'https://cdn.test/existing.mp3', 'Existing', 'Old desc', NULL, '2026-05-01T12:00:00+00:00', NULL, NULL),
                (2, 1, 'legacy-guid', 'https://cdn.test/legacy.mp3', 'Repaired Guid', 'Legacy desc', NULL, '2026-05-02T12:00:00+00:00', NULL, NULL)",
            [],
        )
        .unwrap();
        drop(conn);

        let plan = plan_feed_refresh(FeedRefreshPlanArgs {
            db: db_path,
            feed_id: 1,
            feed_xml: xml_path,
            auto_whitelist_new_posts: true,
        })
        .unwrap();

        assert_eq!(plan.updates["image_url"], "https://img.test/new-feed.png");
        assert_eq!(plan.new_posts.len(), 2);
        assert_eq!(plan.new_posts[0]["guid"], "new-guid");
        assert_eq!(plan.new_posts[0]["duration"], 3723);
        assert_eq!(plan.new_posts[0]["whitelisted"], true);
        assert_eq!(plan.new_posts[1]["guid"], "archive-guid");
        assert_eq!(plan.new_posts[1]["whitelisted"], false);

        assert_eq!(plan.existing_post_updates.len(), 2);
        assert_eq!(plan.existing_post_updates[0]["post_id"], 1);
        assert_eq!(plan.existing_post_updates[0]["title"], "Existing Updated");
        assert_eq!(plan.existing_post_updates[0]["description"], "Changed desc");
        assert_eq!(plan.existing_post_updates[0]["duration"], 321);
        assert_eq!(plan.existing_post_updates[1]["post_id"], 2);
        assert_eq!(plan.existing_post_updates[1]["guid"], "corrected-guid");
    }

    #[test]
    fn render_aggregate_feed_uses_feed_supporter_memberships() {
        let dir = tempfile::tempdir().unwrap();
        let db_path = dir.path().join("podly.sqlite");
        let conn = Connection::open(&db_path).unwrap();
        conn.execute_batch(
            "CREATE TABLE feed (
                id INTEGER PRIMARY KEY,
                title TEXT NOT NULL,
                last_changed_at TEXT
            );
            CREATE TABLE post (
                id INTEGER PRIMARY KEY,
                feed_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                guid TEXT NOT NULL,
                processed_audio_path TEXT,
                description TEXT,
                release_date TEXT,
                duration INTEGER,
                image_url TEXT,
                chapter_data TEXT,
                whitelisted INTEGER NOT NULL
            );
            CREATE TABLE feed_supporter (
                id INTEGER PRIMARY KEY,
                feed_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                created_at TEXT
            );
            CREATE TABLE users (
                id INTEGER PRIMARY KEY,
                username TEXT NOT NULL
            );
            INSERT INTO feed VALUES
                (1, 'Subscribed Feed', '2026-05-08 12:00:00'),
                (2, 'Other Feed', '2026-05-09 12:00:00'),
                (3, 'Second Subscribed Feed', '2026-05-10 12:00:00');
            INSERT INTO post VALUES
                (1, 1, 'Subscribed Episode', 'sub-guid', '/tmp/sub.mp3', 'sub desc',
                    '2026-05-08 12:00:00', 60, NULL, NULL, 1),
                (2, 2, 'Other Episode', 'other-guid', '/tmp/other.mp3', 'other desc',
                    '2026-05-09 12:00:00', 60, NULL, NULL, 1),
                (3, 1, 'Older Episode', 'older-guid', '/tmp/older.mp3', NULL,
                    '2026-05-01 12:00:00', 60, NULL, NULL, 1),
                (4, 1, 'Unlisted Episode', 'unlisted-guid', '/tmp/unlisted.mp3', NULL,
                    '2026-05-12 12:00:00', 60, NULL, NULL, 0),
                (5, 1, 'Unprocessed Episode', 'unprocessed-guid', NULL, NULL,
                    '2026-05-12 12:00:00', 60, NULL, NULL, 1),
                (6, 1, 'Same Date Episode', 'same-date-guid', '/tmp/tie.mp3', NULL,
                    '2026-05-08 12:00:00', 60, NULL, NULL, 1),
                (7, 3, 'Newest Episode', 'newest-guid', '/tmp/newest.mp3', NULL,
                    '2026-05-10 12:00:00', 60.5, NULL, NULL, 1),
                (8, 3, 'Undated Episode', 'undated-guid', '/tmp/undated.mp3', NULL,
                    NULL, 60, NULL, NULL, 1);
            INSERT INTO feed_supporter VALUES
                (1, 1, 42, '2026-05-08 12:00:00'),
                (2, 3, 42, '2026-05-08 12:00:00');
            INSERT INTO users VALUES
                (42, 'listener');",
        )
        .unwrap();
        drop(conn);

        let response = render_aggregate_feed(FeedRenderAggregateArgs {
            db: db_path.clone(),
            user_id: 42,
            base_url: "https://podly.test".to_string(),
            require_auth: true,
            limit_per_feed: 2,
            feed_token: None,
            feed_secret: None,
        })
        .unwrap();

        assert!(response
            .xml
            .contains("[Subscribed Feed] Subscribed Episode"));
        assert!(response
            .xml
            .contains("<title>Podly Podcasts - listener</title>"));
        assert!(response.xml.contains(
            "<description>Aggregate feed for listener - Last 3 processed episodes from each subscribed feed.</description>"
        ));
        assert!(response.xml.contains("sub-guid"));
        assert!(!response.xml.contains("Other Episode"));
        assert!(!response.xml.contains("other-guid"));
        assert_eq!(response.xml.matches("<item>").count(), 4);
        assert!(!response.xml.contains("older-guid"));
        assert!(!response.xml.contains("unlisted-guid"));
        assert!(!response.xml.contains("unprocessed-guid"));
        let newest = response.xml.find("newest-guid").unwrap();
        let same_date = response.xml.find("same-date-guid").unwrap();
        let subscribed = response.xml.find("sub-guid").unwrap();
        let undated = response.xml.find("undated-guid").unwrap();
        assert!(newest < same_date && same_date < subscribed && subscribed < undated);

        let public_response = render_aggregate_feed(FeedRenderAggregateArgs {
            db: db_path,
            user_id: 42,
            base_url: "https://podly.test".to_string(),
            require_auth: false,
            limit_per_feed: 2,
            feed_token: None,
            feed_secret: None,
        })
        .unwrap();
        assert_eq!(public_response.xml.matches("<item>").count(), 5);
        assert!(!public_response.xml.contains("older-guid"));
        assert!(!public_response.xml.contains("unlisted-guid"));
        assert!(!public_response.xml.contains("unprocessed-guid"));
        let newest = public_response.xml.find("newest-guid").unwrap();
        let other = public_response.xml.find("other-guid").unwrap();
        let same_date = public_response.xml.find("same-date-guid").unwrap();
        let subscribed = public_response.xml.find("sub-guid").unwrap();
        let undated = public_response.xml.find("undated-guid").unwrap();
        assert!(
            newest < other && other < same_date && same_date < subscribed && subscribed < undated
        );
    }

    #[test]
    fn jobs_render_matches_python_route_shape_and_ordering() {
        let dir = tempfile::tempdir().unwrap();
        let db_path = dir.path().join("podly.sqlite");
        let conn = Connection::open(&db_path).unwrap();
        conn.execute_batch(
            "CREATE TABLE feed (id INTEGER PRIMARY KEY, title TEXT NOT NULL);
            CREATE TABLE post (
                id INTEGER PRIMARY KEY, feed_id INTEGER NOT NULL, guid TEXT NOT NULL, title TEXT NOT NULL
            );
            CREATE TABLE processing_job (
                id TEXT PRIMARY KEY, post_guid TEXT NOT NULL, status TEXT NOT NULL,
                current_step INTEGER, step_name TEXT, total_steps INTEGER,
                progress_percentage REAL, started_at TEXT, completed_at TEXT,
                error_message TEXT, created_at TEXT, stage_history TEXT,
                ad_windows_count INTEGER, had_classification_parse_error INTEGER NOT NULL DEFAULT 0,
                auto_retry_attempted INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE model_call (
                id INTEGER PRIMARY KEY, post_id INTEGER NOT NULL,
                first_segment_sequence_num INTEGER NOT NULL,
                last_segment_sequence_num INTEGER NOT NULL,
                model_name TEXT NOT NULL, prompt TEXT NOT NULL,
                response TEXT, timestamp TEXT NOT NULL, status TEXT NOT NULL,
                error_message TEXT, retry_attempts INTEGER NOT NULL DEFAULT 0,
                next_retry_at TEXT, service_tier TEXT
            );
            INSERT INTO feed VALUES (1, 'Feed');
            INSERT INTO post VALUES (1, 1, 'guid-1', 'Episode 1');
            INSERT INTO processing_job VALUES
                ('job-complete', 'guid-1', 'completed', 4, 'Done', 4, 100.0, NULL,
                    '2026-05-08 12:02:00', NULL, '2026-05-08 12:02:00',
                    'this is not valid json', 0, 1, 0),
                ('job-pending', 'guid-1', 'pending', 0, 'Queued', 4, 0.0, NULL, NULL, NULL,
                    '2026-05-08 12:01:00', NULL, NULL, 0, 0),
                ('job-running', 'missing-guid', 'running', 2, 'Work', 4, 50.0,
                    '2026-05-08 12:00:00', NULL, NULL, '2026-05-08 12:00:00',
                    '[{\"step\":0,\"step_name\":\"Queued\",\"started_at\":\"2026-05-08T12:00:00\"},{\"step\":2,\"step_name\":\"Work\",\"started_at\":\"2026-05-08T12:00:30\"}]',
                    NULL, 0, 0);
            -- Two tier rows for post 1: a 'flex' classification then a
            -- later 'default' boundary-refine. The Rust summary should
            -- report latest='default' and mixed=true.
            INSERT INTO model_call VALUES
                (1, 1, 0, 100, 'gemini/gemini-3-flash-preview', 'p', 'r',
                    '2026-05-08 11:00:00', 'success', NULL, 1, NULL, 'flex'),
                (2, 1, 101, 110, 'gemini/gemini-3-flash-preview', 'p', 'r',
                    '2026-05-08 11:30:00', 'success', NULL, 1, NULL, 'default');",
        )
        .unwrap();
        drop(conn);

        let active = render_jobs(
            JobsListArgs {
                db: db_path.clone(),
                limit: 10,
            },
            true,
        )
        .unwrap();
        let active_jobs = active["jobs"].as_array().unwrap();
        assert_eq!(active_jobs.len(), 2);
        assert_eq!(active_jobs[0]["job_id"], "job-running");
        assert_eq!(active_jobs[0]["priority"], 2);
        assert_eq!(active_jobs[0]["post_title"], Value::Null);
        // job-running has real JSON history; it must be surfaced as a parsed array.
        let running_history = active_jobs[0]["stage_history"].as_array().unwrap();
        assert_eq!(running_history.len(), 2);
        assert_eq!(running_history[0]["step"], 0);
        assert_eq!(running_history[0]["step_name"], "Queued");
        assert_eq!(running_history[0]["started_at"], "2026-05-08T12:00:00");
        assert_eq!(running_history[1]["step"], 2);
        assert_eq!(running_history[1]["started_at"], "2026-05-08T12:00:30");

        assert_eq!(active_jobs[1]["job_id"], "job-pending");
        assert_eq!(active_jobs[1]["feed_title"], "Feed");
        // job-pending has NULL stage_history; it must surface as an empty array,
        // not null, so the frontend can iterate without a guard.
        assert_eq!(active_jobs[1]["stage_history"], Value::Array(Vec::new()));

        let all = render_jobs(
            JobsListArgs {
                db: db_path,
                limit: 10,
            },
            false,
        )
        .unwrap();
        let all_jobs = all["jobs"].as_array().unwrap();
        assert_eq!(all_jobs.len(), 3);
        assert_eq!(all_jobs[2]["job_id"], "job-complete");
        assert_eq!(all_jobs[2]["completed_at"], "2026-05-08T12:02:00");
        // job-complete has garbage in the column; falling back to [] keeps the
        // listing healthy even if a row was written with malformed JSON.
        assert_eq!(all_jobs[2]["stage_history"], Value::Array(Vec::new()));

        // Zero-ads guard fields surface alongside the rest of the row.
        // job-complete: 0 ads + parse error flag set; auto-retry not attempted.
        assert_eq!(all_jobs[2]["ad_windows_count"], 0);
        assert_eq!(all_jobs[2]["had_classification_parse_error"], true);
        assert_eq!(all_jobs[2]["auto_retry_attempted"], false);
        // job-running: NULL ad_windows_count surfaces as JSON null, not 0.
        assert_eq!(active_jobs[0]["ad_windows_count"], Value::Null);
        assert_eq!(active_jobs[0]["had_classification_parse_error"], false);

        // service_tier summary: the two jobs for post 1 (guid-1) both report
        // the chip with latest='default' (most recent of the two seeded rows)
        // and mixed=true (different tiers across rows).
        let pending_tier = &active_jobs[1]["service_tier"];
        assert_eq!(pending_tier["latest"], "default");
        assert_eq!(pending_tier["label"], "default");
        assert_eq!(pending_tier["mixed"], true);
        // job-running has no matching post in `post` (missing-guid), so no
        // post_id was joined and no service_tier chip should attach.
        assert!(active_jobs[0].get("service_tier").is_none());
    }

    #[test]
    fn jobs_status_mirrors_python_build_run_status_snapshot_shape() {
        let dir = tempfile::tempdir().unwrap();
        let db_path = dir.path().join("podly.sqlite");
        let conn = Connection::open(&db_path).unwrap();
        conn.execute_batch(
            "CREATE TABLE jobs_manager_run (
                id TEXT PRIMARY KEY,
                status TEXT,
                trigger TEXT,
                started_at TEXT,
                completed_at TEXT,
                updated_at TEXT,
                counters_reset_at TEXT,
                context_json TEXT
            );
            CREATE TABLE processing_job (
                id TEXT PRIMARY KEY,
                jobs_manager_run_id TEXT,
                post_guid TEXT NOT NULL,
                status TEXT NOT NULL,
                current_step INTEGER,
                step_name TEXT,
                total_steps INTEGER,
                progress_percentage REAL,
                started_at TEXT,
                completed_at TEXT,
                error_message TEXT,
                created_at TEXT,
                stage_history TEXT
            );
            INSERT INTO jobs_manager_run VALUES (
                'jobs-manager-singleton', 'running', 'manual',
                '2026-05-08 12:00:00', NULL, '2026-05-08 12:00:30',
                '2026-05-08 12:00:00', '{\"last_trigger\":\"manual\"}'
            );
            INSERT INTO processing_job VALUES
                ('j1', 'jobs-manager-singleton', 'g1', 'running', 2, 'Work', 4, 50.0, '2026-05-08 12:00:00', NULL, NULL, '2026-05-08 12:00:00', NULL),
                ('j2', 'jobs-manager-singleton', 'g2', 'pending', 0, 'Queued', 4, 0.0, NULL, NULL, NULL, '2026-05-08 12:00:10', NULL),
                ('j3', 'jobs-manager-singleton', 'g3', 'completed', 4, 'Done', 4, 100.0, '2026-05-08 12:00:00', '2026-05-08 12:00:20', NULL, '2026-05-08 12:00:00', NULL),
                ('j4', 'jobs-manager-singleton', 'g4', 'skipped', 4, 'Skipped', 4, 100.0, NULL, '2026-05-08 12:00:25', NULL, '2026-05-08 12:00:00', NULL),
                ('j5', 'jobs-manager-singleton', 'g5', 'failed', 2, 'Failed', 4, 50.0, '2026-05-08 12:00:00', '2026-05-08 12:00:15', 'boom', '2026-05-08 12:00:00', NULL),
                ('j6', 'jobs-manager-singleton', 'g6', 'cancelled', 0, 'Cancelled', 4, 0.0, NULL, '2026-05-08 12:00:18', 'stopped', '2026-05-08 12:00:00', NULL),
                ('j7', 'other-run', 'g7', 'running', 1, 'Work', 4, 25.0, NULL, NULL, NULL, '2026-05-08 12:00:00', NULL),
                ('j8', 'jobs-manager-singleton', 'g8', 'pending', 0, 'Queued', 4, 0.0, NULL, NULL, NULL, '2026-05-08 11:59:00', NULL);",
        )
        .unwrap();
        drop(conn);

        let response = render_jobs_status(JobsStatusArgs {
            db: db_path.clone(),
        })
        .unwrap();
        let run = response["run"].as_object().unwrap();
        assert_eq!(run["id"], "jobs-manager-singleton");
        // running > 0 → status "running"; idle override only kicks in when
        // queued + running == 0.
        assert_eq!(run["status"], "running");
        assert_eq!(run["trigger"], "manual");
        // started_at gets the ISO-ified version of the SQLite naive timestamp.
        assert_eq!(run["started_at"], "2026-05-08T12:00:00");
        assert_eq!(run["counters_reset_at"], "2026-05-08T12:00:00");
        // Only jobs scoped to this run AND created at/after counters_reset_at
        // count. j7 (other run) and j8 (older created_at) must be excluded.
        assert_eq!(run["queued_jobs"], 1);
        assert_eq!(run["running_jobs"], 1);
        assert_eq!(run["completed_jobs"], 1);
        // failed + cancelled are summed into failed_jobs.
        assert_eq!(run["failed_jobs"], 2);
        assert_eq!(run["skipped_jobs"], 1);
        assert_eq!(run["total_jobs"], 6);
        // (completed + skipped) / total * 100 = 2/6 * 100 ≈ 33.33
        assert!((run["progress_percentage"].as_f64().unwrap() - 33.33).abs() < 0.01);
        // context_json gets parsed back to a real object, not left as a string.
        assert_eq!(run["context"]["last_trigger"], "manual");
    }

    #[test]
    fn jobs_status_returns_null_envelope_when_singleton_row_missing() {
        let dir = tempfile::tempdir().unwrap();
        let db_path = dir.path().join("podly.sqlite");
        let conn = Connection::open(&db_path).unwrap();
        conn.execute_batch(
            "CREATE TABLE jobs_manager_run (
                id TEXT PRIMARY KEY, status TEXT, trigger TEXT,
                started_at TEXT, completed_at TEXT, updated_at TEXT,
                counters_reset_at TEXT, context_json TEXT
            );
            CREATE TABLE processing_job (
                id TEXT PRIMARY KEY, jobs_manager_run_id TEXT,
                post_guid TEXT NOT NULL, status TEXT NOT NULL,
                current_step INTEGER, step_name TEXT, total_steps INTEGER,
                progress_percentage REAL, started_at TEXT, completed_at TEXT,
                error_message TEXT, created_at TEXT, stage_history TEXT
            );",
        )
        .unwrap();
        drop(conn);

        let response = render_jobs_status(JobsStatusArgs { db: db_path }).unwrap();
        assert_eq!(response["run"], Value::Null);
    }

    #[test]
    fn jobs_status_idle_when_no_queued_or_running_jobs() {
        let dir = tempfile::tempdir().unwrap();
        let db_path = dir.path().join("podly.sqlite");
        let conn = Connection::open(&db_path).unwrap();
        conn.execute_batch(
            "CREATE TABLE jobs_manager_run (
                id TEXT PRIMARY KEY, status TEXT, trigger TEXT,
                started_at TEXT, completed_at TEXT, updated_at TEXT,
                counters_reset_at TEXT, context_json TEXT
            );
            CREATE TABLE processing_job (
                id TEXT PRIMARY KEY, jobs_manager_run_id TEXT,
                post_guid TEXT NOT NULL, status TEXT NOT NULL,
                current_step INTEGER, step_name TEXT, total_steps INTEGER,
                progress_percentage REAL, started_at TEXT, completed_at TEXT,
                error_message TEXT, created_at TEXT, stage_history TEXT
            );
            INSERT INTO jobs_manager_run VALUES (
                'jobs-manager-singleton', 'running', 'manual',
                '2026-05-08 12:00:00', NULL, '2026-05-08 12:00:30',
                '2026-05-08 12:00:00', NULL
            );
            INSERT INTO processing_job VALUES
                ('j1', 'jobs-manager-singleton', 'g1', 'completed', 4, 'Done', 4, 100.0,
                    '2026-05-08 12:00:00', '2026-05-08 12:00:20', NULL,
                    '2026-05-08 12:00:00', NULL);",
        )
        .unwrap();
        drop(conn);

        let response = render_jobs_status(JobsStatusArgs { db: db_path }).unwrap();
        let run = response["run"].as_object().unwrap();
        // No queued/running jobs left → idle override regardless of what
        // status the run row carries.
        assert_eq!(run["status"], "pending");
        assert_eq!(run["completed_jobs"], 1);
        assert_eq!(run["queued_jobs"], 0);
        assert_eq!(run["running_jobs"], 0);
        // 1/1 * 100 = 100.0
        assert!((run["progress_percentage"].as_f64().unwrap() - 100.0).abs() < 0.01);
    }

    fn seg(seq: i64, start: f64, end: f64) -> StatsTranscriptSegmentRow {
        StatsTranscriptSegmentRow {
            id: seq,
            sequence_num: seq,
            start_time: start,
            end_time: end,
            text: format!("seg{seq}"),
            speaker_label: None,
        }
    }

    #[test]
    fn wb_context_uses_seq_window_when_provided() {
        let segments: Vec<_> = (0..10)
            .map(|i| seg(i, i as f64 * 10.0, i as f64 * 10.0 + 9.0))
            .collect();
        let selected = select_wb_context_segments(&segments, 100.0, 110.0, Some(5), Some(6));
        let seq_nums: Vec<i64> = selected.iter().map(|s| s.sequence_num).collect();
        // start_seq = max(0, 5-8)=0, end_seq = min(9, 6+8)=9 → [0..=9]
        assert_eq!(seq_nums, vec![0, 1, 2, 3, 4, 5, 6, 7, 8, 9]);
    }

    #[test]
    fn wb_context_clamps_seq_window_to_available_range() {
        let segments: Vec<_> = (5..10)
            .map(|i| seg(i, i as f64 * 10.0, i as f64 * 10.0 + 9.0))
            .collect();
        // first_seq-2 = 3 < min_seq(5), last_seq+2 = 102 > max_seq(9)
        let selected = select_wb_context_segments(&segments, 0.0, 0.0, Some(5), Some(100));
        let seq_nums: Vec<i64> = selected.iter().map(|s| s.sequence_num).collect();
        assert_eq!(seq_nums, vec![5, 6, 7, 8, 9]);
    }

    #[test]
    fn wb_context_falls_back_to_time_overlap_without_seq_window() {
        let segments = vec![
            seg(0, 0.0, 10.0),
            seg(1, 10.0, 20.0),
            seg(2, 20.0, 30.0),
            seg(3, 30.0, 40.0),
            seg(4, 40.0, 50.0),
            seg(5, 50.0, 60.0),
            seg(6, 60.0, 70.0),
            seg(7, 70.0, 80.0),
        ];
        // ad 32-37 overlaps only seg 3 → window [max(0,3-2)..min(8,3+3)] = [1..6]
        let selected = select_wb_context_segments(&segments, 32.0, 37.0, None, None);
        let seq_nums: Vec<i64> = selected.iter().map(|s| s.sequence_num).collect();
        assert_eq!(seq_nums, vec![1, 2, 3, 4, 5]);
    }

    #[test]
    fn wb_context_time_overlap_spans_multiple_segments() {
        let segments = vec![
            seg(0, 0.0, 10.0),
            seg(1, 10.0, 20.0),
            seg(2, 20.0, 30.0),
            seg(3, 30.0, 40.0),
            seg(4, 40.0, 50.0),
        ];
        // ad 15-35 overlaps segs 1,2,3 → first=1,last=3 → [max(0,-1)..min(5,6)] = [0..5]
        let selected = select_wb_context_segments(&segments, 15.0, 35.0, None, None);
        let seq_nums: Vec<i64> = selected.iter().map(|s| s.sequence_num).collect();
        assert_eq!(seq_nums, vec![0, 1, 2, 3, 4]);
    }

    #[test]
    fn wb_context_returns_empty_when_no_overlap_and_no_seq_window() {
        let segments = vec![seg(0, 0.0, 10.0), seg(1, 10.0, 20.0)];
        let selected = select_wb_context_segments(&segments, 100.0, 110.0, None, None);
        assert!(selected.is_empty());
    }

    fn wb_word_value(word: &str, start: f64, end: f64) -> Value {
        json!({"word": word, "start": start, "end": end})
    }

    fn wb_seg_with_words(
        seq: i64,
        start: f64,
        end: f64,
        text: &str,
        words: Vec<Value>,
    ) -> WbSegment {
        WbSegment {
            sequence_num: seq,
            start_time: start,
            end_time: end,
            text: text.to_string(),
            words,
        }
    }

    #[test]
    fn wb_normalize_token_strips_edge_punctuation_and_keeps_internal_apostrophe() {
        assert_eq!(wb_normalize_token("(brought"), "brought");
        assert_eq!(wb_normalize_token("you..."), "you");
        assert_eq!(wb_normalize_token("don't"), "don't");
        assert_eq!(wb_normalize_token(",,,"), "");
    }

    #[test]
    fn wb_split_words_normalizes_each_token() {
        assert_eq!(
            wb_split_words_lower("(brought to you, by a Sponsor!"),
            vec!["brought", "to", "you", "by", "a", "sponsor"]
        );
    }

    #[test]
    fn wb_find_phrase_match_returns_first_for_start_and_last_for_end() {
        let words: Vec<String> = ["alpha", "beta", "gamma", "beta", "delta"]
            .iter()
            .map(|s| s.to_string())
            .collect();
        let phrase: Vec<String> = ["beta"].iter().map(|s| s.to_string()).collect();
        assert_eq!(
            wb_find_phrase_match(&words, &phrase, false, 4),
            Some((1, 1))
        );
        assert_eq!(wb_find_phrase_match(&words, &phrase, true, 4), Some((3, 3)));
    }

    #[test]
    fn wb_find_phrase_match_shrinks_to_partial_when_full_not_found() {
        let words: Vec<String> = ["the", "quick", "brown", "fox"]
            .iter()
            .map(|s| s.to_string())
            .collect();
        // Full phrase "quick fast brown" doesn't fit, but "quick" should match.
        let phrase: Vec<String> = ["quick", "fast", "brown"]
            .iter()
            .map(|s| s.to_string())
            .collect();
        assert_eq!(
            wb_find_phrase_match(&words, &phrase, false, 4),
            Some((1, 1))
        );
    }

    #[test]
    fn wb_estimate_phrase_time_prefers_exact_word_timestamps() {
        let words = vec![
            wb_word_value("this", 100.0, 100.4),
            wb_word_value("is", 100.4, 100.8),
            wb_word_value("brought", 100.8, 101.3),
            wb_word_value("to", 101.3, 101.5),
            wb_word_value("you", 101.5, 101.9),
            wb_word_value("by", 101.9, 102.1),
            wb_word_value("a", 102.1, 102.2),
            wb_word_value("sponsor", 104.75, 105.4),
            wb_word_value("today", 105.4, 106.0),
        ];
        let seg = wb_seg_with_words(
            12,
            100.0,
            110.0,
            "this is brought to you by a sponsor today",
            words,
        );
        let segments = [seg];
        let phrase_tokens = wb_split_words_lower("sponsor today");
        let est = wb_estimate_phrase_time_for_segment(&segments[0], &phrase_tokens, false);
        assert_eq!(est, Some(104.75));
    }

    #[test]
    fn wb_estimate_phrase_time_returns_none_when_phrase_missing() {
        let seg = wb_seg_with_words(12, 100.0, 110.0, "hello world", Vec::new());
        let segments = vec![seg];
        let est =
            wb_estimate_phrase_time(&segments, &[], Some(12), Some("not in the segment"), false);
        assert!(est.is_none());
    }

    #[test]
    fn format_segment_range_label_maps_chapter_sentinels() {
        assert_eq!(
            format_segment_range_label(-100, -100),
            "chapter titles (LLM)"
        );
        assert_eq!(
            format_segment_range_label(-200, -200),
            "chapter topic plan (LLM)"
        );
        // Real transcript ranges fall through to the raw "first-last" form.
        assert_eq!(format_segment_range_label(5, 9), "5-9");
    }

    #[test]
    fn wb_refine_start_clamps_to_max_extension_floor() {
        // Phrase exists but resolves way earlier than orig_ad_start - 30.
        // Without clamp the estimated start would be 0.0; clamp must keep
        // refined_start within 30s of orig.
        let words = vec![
            wb_word_value("sponsor", 0.0, 0.5),
            wb_word_value("today", 0.5, 1.0),
        ];
        let seg = wb_seg_with_words(5, 0.0, 1.0, "sponsor today", words);
        let segments = vec![seg];
        let (refined, changed, err) = wb_refine_start(
            &segments,
            &[],
            100.0,
            Some(5),
            Some("sponsor"),
            None,
            None,
            None,
        );
        assert!(err.is_none());
        assert!(changed);
        // 100 - 30 = 70 is the floor.
        assert!((refined - 70.0).abs() < 1e-9, "got {refined}");
    }

    #[test]
    fn wb_refine_end_clamps_to_max_extension_ceiling() {
        // Phrase resolves well beyond orig + 15; clamp must cap it.
        let words = vec![
            wb_word_value("thanks", 200.0, 200.5),
            wb_word_value("for", 200.5, 201.0),
            wb_word_value("listening", 201.0, 201.5),
        ];
        let seg = wb_seg_with_words(7, 200.0, 201.5, "thanks for listening", words);
        let segments = vec![seg];
        let (refined, changed, err) =
            wb_refine_end(&segments, &[], 100.0, Some(7), Some("listening"));
        assert!(err.is_none());
        assert!(changed);
        // 100 + 15 = 115 is the ceiling.
        assert!((refined - 115.0).abs() < 1e-9, "got {refined}");
    }

    #[test]
    fn wb_refine_from_llm_returns_refined_window_for_well_formed_response() {
        let words_start = vec![
            wb_word_value("this", 100.0, 100.5),
            wb_word_value("episode", 100.5, 101.0),
            wb_word_value("is", 101.0, 101.2),
            wb_word_value("brought", 101.2, 101.6),
            wb_word_value("to", 101.6, 101.8),
            wb_word_value("you", 101.8, 102.0),
            wb_word_value("by", 102.0, 102.3),
        ];
        let words_end = vec![
            wb_word_value("thanks", 130.0, 130.4),
            wb_word_value("for", 130.4, 130.7),
            wb_word_value("listening", 130.7, 131.5),
            wb_word_value("folks", 131.5, 132.0),
        ];
        let segments = vec![
            wb_seg_with_words(
                100,
                100.0,
                110.0,
                "this episode is brought to you by",
                words_start,
            ),
            wb_seg_with_words(101, 125.0, 132.0, "thanks for listening folks", words_end),
        ];
        let content = r#"{
            "refined_start_segment_seq": 100,
            "refined_start_phrase": "brought to you by",
            "refined_end_segment_seq": 101,
            "refined_end_phrase": "thanks for listening",
            "start_adjustment_reason": "sponsor lead-in",
            "end_adjustment_reason": "sign-off"
        }"#;

        let result =
            wb_refine_from_llm_inner(&segments, content, 105.0, 128.0, Some(100), Some(101));

        assert_eq!(result["parse_status"], "ok");
        assert_eq!(result["start_changed"], true);
        assert_eq!(result["end_changed"], true);
        assert!(result["start_error"].is_null());
        assert!(result["end_error"].is_null());
        assert_eq!(result["start_reason"], "sponsor lead-in");
        assert_eq!(result["end_reason"], "sign-off");
        let refined_start = result["refined_start"].as_f64().unwrap();
        let refined_end = result["refined_end"].as_f64().unwrap();
        assert!((refined_start - 101.2).abs() < 1e-6, "got {refined_start}");
        assert!((refined_end - 131.5).abs() < 1e-6, "got {refined_end}");
    }

    #[test]
    fn wb_refine_from_llm_falls_back_when_llm_json_unparseable() {
        let segments = vec![wb_seg_with_words(5, 0.0, 1.0, "x", Vec::new())];
        let result =
            wb_refine_from_llm_inner(&segments, "this is not json at all", 7.0, 12.0, None, None);
        assert_eq!(result["parse_status"], "failed");
        assert_eq!(result["refined_start"].as_f64().unwrap(), 7.0);
        assert_eq!(result["refined_end"].as_f64().unwrap(), 12.0);
        assert_eq!(result["start_changed"], false);
        assert_eq!(result["end_changed"], false);
    }

    #[test]
    fn wb_refine_from_llm_flags_salvaged_partial_fields() {
        // Truncated mid-key: only the start segment seq + phrase survive the
        // regex salvage. Force the salvage path and assert the wrapper marks
        // it accordingly.
        let words = vec![
            wb_word_value("we", 12.0, 12.5),
            wb_word_value("got", 12.5, 12.8),
            wb_word_value("to", 12.8, 13.0),
            wb_word_value("take", 13.0, 13.4),
        ];
        let segments = vec![wb_seg_with_words(
            1356,
            12.0,
            15.0,
            "we got to take a quick pause",
            words,
        )];
        let truncated = r#"garbage prefix "refined_start_segment_seq": 1356, "refined_start_phrase": "we got to take", more garbage"#;
        let result =
            wb_refine_from_llm_inner(&segments, truncated, 14.0, 18.0, Some(1356), Some(1356));
        assert_eq!(result["parse_status"], "salvaged");
        // Start refines to the "we" word time, clamped to orig - 30.
        let refined_start = result["refined_start"].as_f64().unwrap();
        assert!((refined_start - 12.0).abs() < 1e-6, "got {refined_start}");
        assert_eq!(result["start_changed"], true);
    }

    #[test]
    fn wb_refine_from_llm_clamps_to_max_extension() {
        // Phrase resolves way before orig - 30; clamp must hold.
        let words = vec![
            wb_word_value("sponsor", 0.0, 0.5),
            wb_word_value("today", 0.5, 1.0),
        ];
        let segments = vec![wb_seg_with_words(5, 0.0, 1.0, "sponsor today", words)];
        let content = r#"{
            "refined_start_segment_seq": 5,
            "refined_start_phrase": "sponsor",
            "refined_end_segment_seq": 5,
            "refined_end_phrase": null,
            "start_adjustment_reason": "x",
            "end_adjustment_reason": "y"
        }"#;
        let result = wb_refine_from_llm_inner(&segments, content, 100.0, 110.0, Some(5), Some(5));
        assert_eq!(result["parse_status"], "ok");
        let refined_start = result["refined_start"].as_f64().unwrap();
        assert!((refined_start - 70.0).abs() < 1e-9, "got {refined_start}");
    }

    #[test]
    fn wb_refine_start_reports_phrase_not_found() {
        let seg = wb_seg_with_words(5, 100.0, 110.0, "hello there", Vec::new());
        let segments = vec![seg];
        let (refined, changed, err) = wb_refine_start(
            &segments,
            &[],
            100.0,
            Some(5),
            Some("absent phrase"),
            None,
            None,
            None,
        );
        assert_eq!(refined, 100.0);
        assert!(!changed);
        assert_eq!(err.as_deref(), Some("start_phrase_not_found"));
    }

    fn chapter_seg(seq: i64, start_s: f64, end_s: f64, text: &str) -> StatsTranscriptSegmentRow {
        StatsTranscriptSegmentRow {
            id: seq,
            sequence_num: seq,
            start_time: start_s,
            end_time: end_s,
            text: text.to_string(),
            speaker_label: None,
        }
    }

    #[test]
    fn chapter_format_timestamp_matches_python_shape() {
        assert_eq!(chapter_format_timestamp(0), "00:00");
        assert_eq!(chapter_format_timestamp(65_000), "01:05");
        assert_eq!(chapter_format_timestamp(3_665_000), "1:01:05");
    }

    #[test]
    fn chapter_topic_blocks_groups_segments_into_target_size_windows() {
        // 10-minute episode, 5 segments at 2-minute spacing, target ~5 blocks.
        // Window math: total=600_000ms / 5 = 120_000 → max(min=60_000, raw)=120_000 → round-up to 120_000.
        // So each 2-minute segment becomes its own block (next seg crosses window).
        let segments = vec![
            chapter_seg(0, 0.0, 110.0, "alpha block"),
            chapter_seg(1, 120.0, 230.0, "beta block"),
            chapter_seg(2, 240.0, 350.0, "gamma block"),
            chapter_seg(3, 360.0, 470.0, "delta block"),
            chapter_seg(4, 480.0, 590.0, "epsilon block"),
        ];
        let blocks = chapter_build_topic_blocks(&segments, 600_000, 5, 60, 120, 220, false);
        let block_indices: Vec<i64> = blocks
            .iter()
            .map(|b| b["block_index"].as_i64().unwrap())
            .collect();
        assert_eq!(block_indices, vec![0, 1, 2, 3, 4]);
        let first_text = blocks[0]["text"].as_str().unwrap();
        assert_eq!(first_text, "alpha block");
        let first_start = blocks[0]["start_ms"].as_i64().unwrap();
        assert_eq!(first_start, 0);
    }

    #[test]
    fn chapter_topic_blocks_respects_min_block_seconds_floor() {
        // 5 segments inside a 30-second window; raw=30s ≈ below the 60s floor.
        // Window must clamp up to 60s, then rounded up to nearest 30s → 60s.
        let segments = vec![
            chapter_seg(0, 0.0, 5.0, "first"),
            chapter_seg(1, 10.0, 15.0, "second"),
            chapter_seg(2, 20.0, 25.0, "third"),
            chapter_seg(3, 30.0, 35.0, "fourth"),
            chapter_seg(4, 40.0, 50.0, "fifth"),
        ];
        let blocks = chapter_build_topic_blocks(&segments, 50_000, 60, 60, 120, 220, false);
        // All segments fit within the 60s floor → one block.
        assert_eq!(blocks.len(), 1);
        assert_eq!(
            blocks[0]["text"].as_str().unwrap(),
            "first second third fourth fifth"
        );
    }

    #[test]
    fn chapter_topic_blocks_caps_at_max_block_seconds() {
        // Pretend the episode is 6 hours but target_block_count = 1 to force the
        // raw window beyond max_block_seconds (default 120s).
        let segments: Vec<StatsTranscriptSegmentRow> = (0..10)
            .map(|i| chapter_seg(i, (i as f64) * 60.0, (i as f64) * 60.0 + 50.0, "x"))
            .collect();
        let blocks = chapter_build_topic_blocks(&segments, 6 * 3600 * 1000, 1, 60, 120, 220, false);
        // With block_window clamped to 120s and segments spaced 60s, we should
        // end up grouping 2 segments per block roughly. The key assertion: more
        // than one block (cap took effect) and < total segments.
        assert!(
            blocks.len() > 1 && blocks.len() < segments.len(),
            "got {}",
            blocks.len()
        );
    }

    #[test]
    fn chapter_topic_blocks_clip_text_at_max_chars_per_block() {
        let segments = vec![
            chapter_seg(0, 0.0, 1.0, &"a".repeat(500)),
            chapter_seg(1, 1.0, 2.0, &"b".repeat(500)),
        ];
        let blocks = chapter_build_topic_blocks(&segments, 2_000, 60, 60, 120, 220, false);
        assert_eq!(blocks.len(), 1);
        let text = blocks[0]["text"].as_str().unwrap();
        // Head+middle split: the head is 'a's from the front, and the middle
        // window straddles the boundary between the 'a' and 'b' segments —
        // proving the LLM now sees substantive mid-block content, not just
        // the opening.
        assert!(text.chars().count() <= 220);
        assert!(text.starts_with('a'));
        assert!(text.contains(" ... "));
        assert!(text.contains('b'));
    }

    #[test]
    fn chapter_truncate_block_text_takes_head_and_middle_window() {
        // Place a distinctive token at the block's midpoint so we can verify
        // the middle window actually captures mid-block content instead of
        // discarding it (the old front-only truncation would drop it).
        let lead = "a".repeat(400);
        let signal = " RIVERFRONT_SIGNAL ";
        let tail = "b".repeat(400);
        let text = format!("{lead}{signal}{tail}");
        let truncated = chapter_truncate_block_text(&text, 220);
        assert!(truncated.chars().count() <= 220);
        assert!(truncated.starts_with('a'));
        assert!(truncated.contains(" ... "));
        assert!(truncated.contains("RIVERFRONT_SIGNAL"));
    }

    #[test]
    fn chapter_truncate_block_text_returns_full_text_when_under_budget() {
        let text = "short block";
        assert_eq!(chapter_truncate_block_text(text, 220), text);
    }

    #[test]
    fn chapter_topic_blocks_normalizes_internal_whitespace() {
        let segments = vec![chapter_seg(0, 0.0, 10.0, "hello    world\n\nthere")];
        let blocks = chapter_build_topic_blocks(&segments, 10_000, 60, 60, 120, 220, false);
        assert_eq!(blocks.len(), 1);
        assert_eq!(blocks[0]["text"].as_str().unwrap(), "hello world there");
    }

    #[test]
    fn chapter_topic_blocks_include_segment_markers() {
        let segments = vec![
            chapter_seg(7, 0.0, 20.0, "Intro and setup"),
            chapter_seg(8, 21.0, 40.0, "More intro"),
        ];
        let blocks = chapter_build_topic_blocks(&segments, 60_000, 60, 60, 120, 220, true);
        assert_eq!(blocks.len(), 1);
        assert_eq!(
            blocks[0]["text"].as_str().unwrap(),
            "[s7] Intro and setup [s8] More intro"
        );
    }

    #[test]
    fn chapter_truncate_block_parts_keeps_markers_intact() {
        let parts: Vec<String> = (0..40)
            .map(|i| format!("[s{i}] segment number {i} text body"))
            .collect();
        let out = chapter_truncate_block_parts(&parts, 200);
        assert!(out.chars().count() <= 200);
        assert!(out.contains(" ... "));
        assert!(out.starts_with("[s0] "));
        // No partially-cut markers: every '[' must close before the string ends.
        let dangling = regex::Regex::new(r"\[[^\]]*$").unwrap();
        assert!(dangling.find(&out).is_none());
    }

    #[test]
    fn chapter_truncate_block_parts_zero_budget_means_full_text() {
        let parts: Vec<String> = (0..10)
            .map(|i| format!("[s{i}] words and more words"))
            .collect();
        let joined = parts.join(" ");
        assert_eq!(chapter_truncate_block_parts(&parts, 0), joined);
    }

    #[test]
    fn chapter_topic_blocks_returns_empty_on_empty_segments() {
        assert!(chapter_build_topic_blocks(&[], 60_000, 60, 60, 120, 220, false).is_empty());
    }

    #[test]
    fn chapter_segment_filter_drops_segments_overlapping_removed_windows() {
        let segments = vec![
            chapter_seg(0, 0.0, 5.0, "keep"),
            chapter_seg(1, 10.0, 20.0, "drop"),
            chapter_seg(2, 25.0, 30.0, "keep"),
            chapter_seg(3, 40.0, 50.0, "drop"),
            chapter_seg(4, 60.0, 70.0, "keep"),
        ];
        // Two removed windows: 9.5-21s and 39-51s.
        let kept =
            filter_segments_by_removed_windows(&segments, &[(9_500, 21_000), (39_000, 51_000)]);
        let kept_text: Vec<&str> = kept.iter().map(|s| s.text.as_str()).collect();
        assert_eq!(kept_text, vec!["keep", "keep", "keep"]);
    }

    #[test]
    fn chapter_segment_filter_is_robust_to_unsorted_input_windows() {
        let segments = vec![
            chapter_seg(0, 0.0, 5.0, "keep"),
            chapter_seg(1, 10.0, 20.0, "drop"),
            chapter_seg(2, 30.0, 40.0, "drop"),
        ];
        // Same windows as above but reversed — filter must sort internally.
        let kept =
            filter_segments_by_removed_windows(&segments, &[(29_000, 41_000), (9_000, 21_000)]);
        let kept_text: Vec<&str> = kept.iter().map(|s| s.text.as_str()).collect();
        assert_eq!(kept_text, vec!["keep"]);
    }

    #[test]
    fn topic_plan_parse_handles_well_formed_json() {
        let content = r#"{
  "chapter_count": 3,
  "chapters": [
    {"block_index": 0, "title": "Intro"},
    {"block_index": 5, "title": "Middle"},
    {"block_index": 12, "title": "Outro"}
  ]
}"#;
        let result = parse_topic_chapter_plan(content);
        assert!(!result.salvaged);
        assert_eq!(result.expected_count, Some(3));
        assert!(!result.count_mismatch);
        assert_eq!(
            result.entries,
            vec![
                (0, "Intro".to_string()),
                (5, "Middle".to_string()),
                (12, "Outro".to_string()),
            ]
        );
    }

    #[test]
    fn topic_plan_parse_strips_json_code_fences() {
        let content = "```json\n{\"chapter_count\": 1, \"chapters\": [{\"block_index\": 0, \"title\": \"Hello\"}]}\n```";
        let result = parse_topic_chapter_plan(content);
        assert!(!result.salvaged);
        assert_eq!(result.entries, vec![(0, "Hello".to_string())]);
    }

    #[test]
    fn topic_plan_parse_salvages_truncated_response() {
        // Truncated: missing closing `]}` so json.loads fails. Two complete
        // {block_index, title} pairs should still be salvaged.
        let content = r#"{
  "chapter_count": 5,
  "chapters": [
    {"block_index": 0, "title": "Opening"},
    {"block_index": 2, "title": "Topic A"},
    {"block_index": 4, "title": "Topi"#;
        let result = parse_topic_chapter_plan(content);
        assert!(result.salvaged);
        assert_eq!(result.expected_count, Some(5));
        assert_eq!(
            result.entries,
            vec![(0, "Opening".to_string()), (2, "Topic A".to_string()),]
        );
    }

    #[test]
    fn topic_plan_parse_dedupes_duplicate_block_index_during_salvage() {
        let content = r#"
{"block_index": 0, "title": "first"}
{"block_index": 0, "title": "duplicate"}
{"block_index": 1, "title": "second"}
"#;
        let result = parse_topic_chapter_plan(content);
        assert!(result.salvaged);
        assert_eq!(
            result.entries,
            vec![(0, "first".to_string()), (1, "second".to_string())],
        );
    }

    #[test]
    fn topic_plan_parse_reports_count_mismatch_when_chapter_count_disagrees() {
        let content = r#"{
  "chapter_count": 5,
  "chapters": [
    {"block_index": 0, "title": "x"},
    {"block_index": 1, "title": "y"}
  ]
}"#;
        let result = parse_topic_chapter_plan(content);
        assert!(!result.salvaged);
        assert!(result.count_mismatch);
        assert_eq!(result.expected_count, Some(5));
        assert_eq!(result.entries.len(), 2);
    }

    #[test]
    fn topic_plan_parse_returns_empty_on_missing_chapters_key() {
        let content = r#"{"chapter_count": 0, "foo": []}"#;
        let result = parse_topic_chapter_plan(content);
        assert!(!result.salvaged);
        assert!(result.entries.is_empty());
    }

    #[test]
    fn topic_plan_parse_skips_invalid_block_index_entries() {
        let content = r#"{"chapters": [
            {"block_index": 0, "title": "ok"},
            {"block_index": "bad", "title": "skip"},
            {"title": "no-index"},
            {"block_index": 2, "title": "  trimmed  "}
        ]}"#;
        let result = parse_topic_chapter_plan(content);
        assert!(!result.salvaged);
        // The "bad" string DOES parse as i64::FromStr (returns Err) so it skips;
        // missing index skips; trim happens on whitespace-only titles.
        assert_eq!(
            result.entries,
            vec![(0, "ok".to_string()), (2, "trimmed".to_string())]
        );
    }

    fn plan_block(idx: i64, start_ms: i64, text: &str) -> TopicPlanApplyBlock {
        TopicPlanApplyBlock {
            block_index: idx,
            start_ms,
            text: text.to_string(),
        }
    }

    #[test]
    fn topic_plan_apply_assigns_contiguous_end_times_from_next_start() {
        let blocks = vec![
            plan_block(0, 0, "Intro"),
            plan_block(5, 300_000, "Middle"),
            plan_block(12, 720_000, "Outro"),
        ];
        let plan = vec![
            (0, "Opening".to_string()),
            (5, "Middle topic".to_string()),
            (12, "Closing".to_string()),
        ];
        let chapters = chapters_from_topic_plan(&plan, &blocks, 900_000, 0);
        assert_eq!(chapters.len(), 3);
        assert_eq!(chapters[0]["start_time_ms"], 0);
        assert_eq!(chapters[0]["end_time_ms"], 300_000);
        assert_eq!(chapters[1]["start_time_ms"], 300_000);
        assert_eq!(chapters[1]["end_time_ms"], 720_000);
        assert_eq!(chapters[2]["start_time_ms"], 720_000);
        assert_eq!(chapters[2]["end_time_ms"], 900_000);
        assert_eq!(chapters[0]["element_id"], "tgen0");
        assert_eq!(chapters[2]["element_id"], "tgen2");
    }

    #[test]
    fn topic_plan_apply_inserts_missing_block_zero_with_derived_title() {
        let blocks = vec![
            plan_block(0, 0, "Welcome to the show, today we discuss things"),
            plan_block(3, 120_000, "Now back to it"),
        ];
        // Plan omits block 0; normalize must insert it with a title derived
        // from block 0's text.
        let plan = vec![(3, "Main topic".to_string())];
        let chapters = chapters_from_topic_plan(&plan, &blocks, 240_000, 0);
        assert_eq!(chapters.len(), 2);
        assert_eq!(chapters[0]["start_time_ms"], 0);
        // First 8 words, leading non-word chars stripped, no trailing punct.
        // "Welcome to the show, today we discuss things" → 8 words → "Welcome to the show, today we discuss things"
        let title = chapters[0]["title"].as_str().unwrap();
        assert!(title.starts_with("Welcome to the show"));
    }

    #[test]
    fn topic_plan_apply_filters_chapters_below_min_gap_except_last() {
        let blocks = vec![
            plan_block(0, 0, "a"),
            plan_block(1, 30_000, "b"), // too close to 0 → dropped
            plan_block(5, 300_000, "c"),
            plan_block(10, 600_000, "d"),
        ];
        let plan = vec![
            (0, "first".to_string()),
            (1, "filtered".to_string()),
            (5, "kept".to_string()),
            (10, "last".to_string()),
        ];
        let chapters = chapters_from_topic_plan(&plan, &blocks, 900_000, 120_000);
        let starts: Vec<i64> = chapters
            .iter()
            .map(|c| c["start_time_ms"].as_i64().unwrap())
            .collect();
        // block 1 (gap=30s) filtered; block 5 (gap=300s) kept; block 10 last → kept.
        assert_eq!(starts, vec![0, 300_000, 600_000]);
    }

    #[test]
    fn topic_plan_apply_rejects_single_chapter_for_long_episode() {
        let blocks = vec![plan_block(0, 0, "intro")];
        let plan = vec![(0, "only".to_string())];
        // 20+ minute episode with only one resulting chapter → reject.
        let chapters = chapters_from_topic_plan(&plan, &blocks, 30 * 60 * 1000, 0);
        assert!(chapters.is_empty());
    }

    #[test]
    fn topic_plan_apply_allows_single_chapter_for_short_episode() {
        let blocks = vec![plan_block(0, 0, "intro")];
        let plan = vec![(0, "only".to_string())];
        let chapters = chapters_from_topic_plan(&plan, &blocks, 5 * 60 * 1000, 0);
        assert_eq!(chapters.len(), 1);
        assert_eq!(chapters[0]["end_time_ms"], 5 * 60 * 1000);
    }

    #[test]
    fn topic_plan_apply_drops_invalid_block_indexes() {
        let blocks = vec![plan_block(0, 0, "intro"), plan_block(5, 300_000, "mid")];
        let plan = vec![
            (0, "x".to_string()),
            (99, "invalid".to_string()),
            (5, "y".to_string()),
        ];
        let chapters = chapters_from_topic_plan(&plan, &blocks, 600_000, 0);
        assert_eq!(chapters.len(), 2);
        assert_eq!(chapters[0]["title"], "x");
        assert_eq!(chapters[1]["title"], "y");
    }

    #[test]
    fn topic_plan_apply_falls_back_to_derived_title_when_llm_title_empty() {
        let blocks = vec![
            plan_block(0, 0, "alpha beta gamma delta epsilon zeta eta theta iota"),
            plan_block(5, 300_000, "next chapter content here"),
        ];
        let plan = vec![
            (0, "  ".to_string()), // whitespace-only title
            (5, "real".to_string()),
        ];
        let chapters = chapters_from_topic_plan(&plan, &blocks, 600_000, 0);
        let first_title = chapters[0]["title"].as_str().unwrap();
        // Derived from block 0 text — first 8 words plus "..." since there are 9.
        assert!(first_title.starts_with("alpha beta gamma delta"));
        assert!(first_title.ends_with("..."));
    }

    #[test]
    fn chapter_title_from_text_handles_empty_and_punctuation_only() {
        assert_eq!(chapter_title_from_text("", 3), "Chapter 3");
        assert_eq!(chapter_title_from_text("   ...   ", 7), "Chapter 7");
    }

    #[test]
    fn chapter_segment_filter_returns_empty_when_all_overlap() {
        let segments = vec![
            chapter_seg(0, 0.0, 5.0, "a"),
            chapter_seg(1, 10.0, 20.0, "b"),
        ];
        let kept = filter_segments_by_removed_windows(&segments, &[(0, 25_000)]);
        assert!(kept.is_empty());
    }

    #[test]
    fn wb_json_parse_handles_well_formed_response() {
        let content = r#"{
  "refined_start_segment_seq": 1,
  "refined_start_phrase": "brought to you by",
  "refined_end_segment_seq": 3,
  "refined_end_phrase": "thanks for listening",
  "start_adjustment_reason": "sponsor lead-in",
  "end_adjustment_reason": "sign-off"
}"#;
        let (parsed, salvaged) = wb_parse_json_full(content);
        assert!(!salvaged);
        let obj = parsed.expect("parse failed").as_object().unwrap().clone();
        assert_eq!(
            obj.get("refined_start_segment_seq")
                .and_then(|v| v.as_i64()),
            Some(1)
        );
        assert_eq!(
            obj.get("refined_end_segment_seq").and_then(|v| v.as_i64()),
            Some(3)
        );
        assert_eq!(
            obj.get("refined_end_phrase").and_then(|v| v.as_str()),
            Some("thanks for listening")
        );
    }

    #[test]
    fn wb_json_parse_strips_code_fences() {
        let content =
            "```json\n{\"refined_start_segment_seq\": 7, \"refined_end_segment_seq\": 9}\n```";
        let (parsed, salvaged) = wb_parse_json_full(content);
        assert!(!salvaged);
        let obj = parsed.unwrap().as_object().unwrap().clone();
        assert_eq!(
            obj.get("refined_start_segment_seq")
                .and_then(|v| v.as_i64()),
            Some(7)
        );
        assert_eq!(
            obj.get("refined_end_segment_seq").and_then(|v| v.as_i64()),
            Some(9)
        );
    }

    #[test]
    fn wb_json_parse_repairs_truncated_fenced_payload() {
        // Mirrors `test_parse_json_recovers_truncated_fenced_payload`.
        let content = "```json\n{\n  \"refined_start_segment_seq\": 2096,\n  \"refined_start_phrase\": \"if you're the purchasing\",\n  \"refined_end_segment_seq\": 2115,\n  \"refined_end_phrase\": \"thank you\",\n  \"start_adjustment_reason\": \"start moved to sponsor lead in\",\n  \"end_adjustment_reason\": \"end kept near return cue\"\n";
        let (parsed, _salvaged) = wb_parse_json_full(content);
        let obj = parsed.expect("repair failed").as_object().unwrap().clone();
        assert_eq!(
            obj.get("refined_start_segment_seq")
                .and_then(|v| v.as_i64()),
            Some(2096)
        );
        assert_eq!(
            obj.get("refined_end_segment_seq").and_then(|v| v.as_i64()),
            Some(2115)
        );
        assert_eq!(
            obj.get("refined_end_phrase").and_then(|v| v.as_str()),
            Some("thank you")
        );
    }

    #[test]
    fn wb_json_parse_recovers_partial_fields_when_value_truncated_mid_key() {
        // Mirrors `test_parse_json_recovers_truncated_mid_key_prefix`.
        let content = "{\n  \"refined_start_segment_seq\": 288,\n  \"refined_start_phrase";
        let (parsed, _salvaged) = wb_parse_json_full(content);
        let obj = parsed.unwrap().as_object().unwrap().clone();
        assert_eq!(
            obj.get("refined_start_segment_seq")
                .and_then(|v| v.as_i64()),
            Some(288)
        );
        // The truncated `refined_start_phrase` key should not appear.
        assert!(!obj.contains_key("refined_start_phrase"));
    }

    #[test]
    fn wb_json_parse_returns_partial_fields_when_full_parse_fails() {
        // Garbage JSON but with a recognisable refined_start_segment_seq pair —
        // the partial-fields regex must still emit it. salvaged=true.
        let content = r#"garbage prefix "refined_start_segment_seq": 17, more garbage "refined_end_phrase": "end here""#;
        let (parsed, salvaged) = wb_parse_json_full(content);
        assert!(
            salvaged,
            "expected salvage to be flagged for partial fields"
        );
        let obj = parsed.unwrap().as_object().unwrap().clone();
        assert_eq!(
            obj.get("refined_start_segment_seq")
                .and_then(|v| v.as_i64()),
            Some(17)
        );
        assert_eq!(
            obj.get("refined_end_phrase").and_then(|v| v.as_str()),
            Some("end here")
        );
    }

    #[test]
    fn wb_json_parse_emits_null_when_refined_seq_is_explicit_null() {
        let content = r#"{"refined_start_segment_seq": null, "refined_end_segment_seq": 5}"#;
        let (parsed, salvaged) = wb_parse_json_full(content);
        assert!(!salvaged);
        let obj = parsed.unwrap().as_object().unwrap().clone();
        assert!(obj.get("refined_start_segment_seq").unwrap().is_null());
        assert_eq!(
            obj.get("refined_end_segment_seq").and_then(|v| v.as_i64()),
            Some(5)
        );
    }

    #[test]
    fn wb_json_parse_returns_none_for_empty_input() {
        let (parsed, salvaged) = wb_parse_json_full("");
        assert!(parsed.is_none());
        assert!(!salvaged);
    }

    #[test]
    fn wb_extract_json_objects_returns_each_top_level_object() {
        let text = r#"prefix {"a": 1} mid {"b": {"c": 2}} suffix"#;
        let out = wb_extract_json_objects(text);
        assert_eq!(out, vec![r#"{"a": 1}"#, r#"{"b": {"c": 2}}"#]);
    }

    #[test]
    fn wb_repair_truncated_json_drops_dangling_pair_and_balances_braces() {
        let input = r#"{"a": 1, "b": "incomplete"#;
        let repaired = wb_repair_truncated_json(input).expect("should repair");
        // After repair, json.loads should succeed.
        let parsed: serde_json::Result<Value> = serde_json::from_str(&repaired);
        assert!(
            parsed.is_ok(),
            "repaired output should be valid JSON: {repaired}"
        );
        let obj = parsed.unwrap().as_object().unwrap().clone();
        assert_eq!(obj.get("a").and_then(|v| v.as_i64()), Some(1));
        assert!(!obj.contains_key("b"));
    }

    #[test]
    fn wb_refine_end_uses_segment_boundary_when_phrase_absent() {
        let seg = wb_seg_with_words(345, 200.0, 205.0, "text", Vec::new());
        let segments = vec![seg];
        let (refined, changed, err) = wb_refine_end(&segments, &[], 204.0, Some(345), None);
        assert!(err.is_none());
        assert!(changed);
        assert_eq!(refined, 205.0);
    }

    fn seed_costs_db(db_path: &Path) {
        let conn = Connection::open(db_path).unwrap();
        conn.execute_batch(
            "CREATE TABLE feed (id INTEGER PRIMARY KEY, title TEXT);
             CREATE TABLE users (
                id INTEGER PRIMARY KEY, username TEXT NOT NULL, role TEXT NOT NULL,
                feed_subscription_status TEXT NOT NULL, stripe_subscription_id TEXT
             );
             CREATE TABLE feed_supporter (
                id INTEGER PRIMARY KEY, feed_id INTEGER NOT NULL, user_id INTEGER NOT NULL
             );
             CREATE TABLE post (
                id INTEGER PRIMARY KEY, feed_id INTEGER NOT NULL, guid TEXT NOT NULL,
                duration REAL
             );
             CREATE TABLE processing_job (
                id TEXT PRIMARY KEY, post_guid TEXT NOT NULL, status TEXT NOT NULL,
                completed_at TEXT
             );
             CREATE TABLE transcript_segment (
                id INTEGER PRIMARY KEY, post_id INTEGER NOT NULL, sequence_num INTEGER,
                start_time REAL NOT NULL, end_time REAL NOT NULL, text TEXT, speaker_label TEXT
             );
             CREATE TABLE identification (
                id INTEGER PRIMARY KEY, transcript_segment_id INTEGER NOT NULL,
                model_call_id INTEGER, confidence REAL, label TEXT NOT NULL
             );
             CREATE TABLE model_call (
                id INTEGER PRIMARY KEY, post_id INTEGER NOT NULL,
                first_segment_sequence_num INTEGER NOT NULL,
                last_segment_sequence_num INTEGER NOT NULL,
                model_name TEXT NOT NULL, prompt TEXT NOT NULL, response TEXT,
                timestamp TEXT, status TEXT NOT NULL, error_message TEXT,
                retry_attempts INTEGER, next_retry_at TEXT, service_tier TEXT,
                prompt_tokens INTEGER, cached_prompt_tokens INTEGER,
                completion_tokens INTEGER, total_tokens INTEGER
             );",
        )
        .unwrap();
        conn.execute("INSERT INTO feed VALUES (1, 'Cost Feed')", [])
            .unwrap();
        conn.execute(
            "INSERT INTO users VALUES (1, 'a', 'user', 'active', NULL), (2, 'b', 'user', 'active', 'sub_x')",
            [],
        )
        .unwrap();
        conn.execute("INSERT INTO feed_supporter VALUES (1, 1, 1), (2, 1, 2)", [])
            .unwrap();
        conn.execute("INSERT INTO post VALUES (1, 1, 'cost-guid', 3600.0)", [])
            .unwrap();
        conn.execute(
            "INSERT INTO processing_job VALUES ('job-1', 'cost-guid', 'completed', '2026-05-15 12:00:00')",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO model_call VALUES
                (1, 1, 0, 1, 'gpt-4o-mini', 'classify', 'resp', '2026-05-15 12:00:01', 'success', NULL, 0, NULL, NULL, 1000000, 500000, 250000, 1250000),
                (2, 1, 0, -1, 'whisper-large-v3-turbo', 'Whisper transcription job', NULL, '2026-05-15 12:00:02', 'success', NULL, 0, NULL, NULL, NULL, NULL, NULL, NULL),
                (3, 1, 0, -1, 'ina:speech_music_noise', 'INA', NULL, '2026-05-15 12:00:03', 'success', NULL, 0, NULL, NULL, NULL, NULL, NULL, NULL)",
            [],
        )
        .unwrap();
    }

    fn write_costs_rates_json(dir: &Path) -> PathBuf {
        // 1.5e-7 input, 7.5e-8 cached, 6e-7 output → matches the Python parity
        // test that exercises gpt-4o-mini with 1M prompt tokens (500k cached)
        // and 250k completion tokens to 0.2625.
        let path = dir.join("rates.json");
        fs::write(
            &path,
            r#"{
              "rates": {
                "gpt-4o-mini|default": {"input": 0.00000015, "cached_input": 0.000000075, "output": 0.0000006}
              },
              "whisper_rate_per_hour": 0.04,
              "ina_rate_per_hour": 0.02,
              "legacy_rate_per_hour": 0.04
            }"#,
        )
        .unwrap();
        path
    }

    #[test]
    fn costs_render_admin_matches_python_dashboard_shape() {
        let dir = tempfile::tempdir().unwrap();
        let db_path = dir.path().join("podly.sqlite");
        seed_costs_db(&db_path);
        let rates_json = write_costs_rates_json(dir.path());

        let payload = render_admin_costs(CostsRenderAdminArgs {
            db: db_path,
            year: 2026,
            month: 5,
            rates_json,
        })
        .unwrap();

        assert_eq!(payload["year"], 2026);
        assert_eq!(payload["month"], 5);
        assert_eq!(payload["whisper_cost_rate_per_hour"], 0.04);
        assert_eq!(payload["ina_cost_rate_per_hour"], 0.02);
        assert_eq!(payload["total_llm_cost"], 0.2625);
        assert_eq!(payload["total_whisper_cost"], 0.04);
        assert_eq!(payload["total_ina_cost"], 0.02);
        assert_eq!(payload["total_cost"], 0.3225);
        // Post duration 3600s = 1 audio hour. No ad time in the fixture, so
        // the total equals the cut duration.
        assert_eq!(payload["total_audio_hours"], 1.0);
        let feeds = payload["feeds"].as_array().unwrap();
        assert_eq!(feeds.len(), 1);
        assert_eq!(feeds[0]["llm_cost"], 0.2625);
        assert_eq!(feeds[0]["whisper_cost"], 0.04);
        assert_eq!(feeds[0]["ina_cost"], 0.02);
        assert_eq!(feeds[0]["episodes_this_month"], 1);
        assert_eq!(feeds[0]["subscriber_count"], 2);
        let users = payload["users"].as_array().unwrap();
        assert_eq!(users.len(), 2);
        // 0.3225 / 2 subscribers → 0.16125 → Python-compatible ties-to-even.
        assert_eq!(users[0]["monthly_cost"], 0.1612);
        assert_eq!(users[1]["monthly_cost"], 0.1612);
        // Stripe enrichment is left to the Python wrapper; sidecar returns null.
        assert_eq!(users[1]["stripe_subscription_id"], "sub_x");
        assert_eq!(users[1]["subscription_amount_cents"], Value::Null);
    }

    #[test]
    fn costs_render_admin_skips_whisper_and_ina_when_no_matching_calls() {
        let dir = tempfile::tempdir().unwrap();
        let db_path = dir.path().join("podly.sqlite");
        seed_costs_db(&db_path);
        // Drop the whisper + INA rows so only the LLM call remains.
        let conn = Connection::open(&db_path).unwrap();
        conn.execute("DELETE FROM model_call WHERE id IN (2, 3)", [])
            .unwrap();
        drop(conn);
        let rates_json = write_costs_rates_json(dir.path());

        let payload = render_admin_costs(CostsRenderAdminArgs {
            db: db_path,
            year: 2026,
            month: 5,
            rates_json,
        })
        .unwrap();

        assert_eq!(payload["total_whisper_cost"], 0.0);
        assert_eq!(payload["total_ina_cost"], 0.0);
        assert_eq!(payload["total_llm_cost"], 0.2625);
    }

    #[test]
    fn costs_render_calls_paginates_in_timestamp_desc_with_estimated_cost() {
        let dir = tempfile::tempdir().unwrap();
        let db_path = dir.path().join("podly.sqlite");
        seed_costs_db(&db_path);
        let rates_json = write_costs_rates_json(dir.path());

        let payload = render_admin_costs_calls(CostsRenderCallsArgs {
            db: db_path,
            page: 1,
            per_page: 10,
            rates_json,
        })
        .unwrap();

        assert_eq!(payload["total"], 3);
        assert_eq!(payload["page"], 1);
        assert_eq!(payload["per_page"], 10);
        assert_eq!(payload["pages"], 1);
        let calls = payload["calls"].as_array().unwrap();
        assert_eq!(calls.len(), 3);
        // timestamp DESC: id 3 (ina) → 2 (whisper) → 1 (gpt-4o-mini)
        assert_eq!(calls[0]["model_name"], "ina:speech_music_noise");
        assert_eq!(calls[0]["estimated_cost"], 0.0);
        assert_eq!(calls[1]["model_name"], "whisper-large-v3-turbo");
        assert_eq!(calls[1]["estimated_cost"], 0.0);
        assert_eq!(calls[2]["model_name"], "gpt-4o-mini");
        assert_eq!(calls[2]["estimated_cost"], 0.2625);
        // ISO format mirrors Python's datetime.isoformat() (space → 'T').
        assert_eq!(calls[2]["timestamp"], "2026-05-15T12:00:01");
    }

    #[test]
    fn urlencoding_simple_matches_python_quote_safe_empty() {
        // Matches behavior of Python's urllib.parse.quote(s, safe='').
        // Unreserved chars (RFC 3986: ALPHA / DIGIT / - _ . ~) pass through;
        // everything else (including '/' and '?') becomes %XX.
        assert_eq!(urlencoding_simple("guid-1"), "guid-1");
        assert_eq!(
            urlencoding_simple("https://searchengine.supercast.tech/episodes/1847512?key=abc"),
            "https%3A%2F%2Fsearchengine.supercast.tech%2Fepisodes%2F1847512%3Fkey%3Dabc"
        );
    }

    #[test]
    fn wb_context_falls_through_to_time_overlap_when_seq_window_empty() {
        // Provide a seq window that picks nothing (segments are seq 0-2,
        // window asks for 100..200 → empty → fall through to time overlap).
        let segments = vec![seg(0, 0.0, 10.0), seg(1, 10.0, 20.0), seg(2, 20.0, 30.0)];
        let selected = select_wb_context_segments(&segments, 5.0, 8.0, Some(100), Some(200));
        // Time overlap: seg 0 overlaps → window [max(0,-2)..min(3,3)] = [0..3]
        let seq_nums: Vec<i64> = selected.iter().map(|s| s.sequence_num).collect();
        assert_eq!(seq_nums, vec![0, 1, 2]);
    }

    fn rseg(seq: i64, start: f64, text: &str) -> StatsTranscriptSegmentRow {
        StatsTranscriptSegmentRow {
            id: seq,
            sequence_num: seq,
            start_time: start,
            end_time: start + 4.0,
            text: text.to_string(),
            speaker_label: None,
        }
    }

    const RAD_AD: [&str; 4] = [
        "Elevate your gaming performance with Alienware deals.",
        "Buy any Alienware PC and get fifty percent off.",
        "Head over to alienware dot com slash deals today.",
        "Back to the show.",
    ];

    fn repeat_ad_transcript() -> Vec<StatsTranscriptSegmentRow> {
        let mut segments = Vec::new();
        for (offset, text) in RAD_AD.iter().enumerate() {
            segments.push(rseg(10 + offset as i64, 100.0 + offset as f64 * 4.0, text));
        }
        for seq in 14..30 {
            segments.push(rseg(
                seq,
                200.0 + seq as f64,
                "Just regular show talk here.",
            ));
        }
        for (offset, text) in RAD_AD.iter().enumerate() {
            segments.push(rseg(30 + offset as i64, 500.0 + offset as f64 * 4.0, text));
        }
        segments
    }

    #[test]
    fn repeat_ad_similarity_bounds() {
        let a = repeat_ad_tokenize("alpha beta gamma");
        assert_eq!(repeat_ad_similarity(&a, &a), 1.0);
        let b = repeat_ad_tokenize("totally different words");
        assert_eq!(repeat_ad_similarity(&a, &b), 0.0);
    }

    #[test]
    fn repeat_ad_finds_verbatim_repeat() {
        let segments = repeat_ad_transcript();
        let candidates = find_repeat_ad_candidates(&segments, 10, 13, &[], 0.85);
        assert_eq!(candidates.len(), 1);
        assert_eq!(candidates[0].first_seq, 30);
        assert_eq!(candidates[0].last_seq, 33);
        assert!(candidates[0].similarity >= 0.85);
    }

    #[test]
    fn repeat_ad_excludes_provided_ranges() {
        let segments = repeat_ad_transcript();
        let candidates = find_repeat_ad_candidates(&segments, 10, 13, &[(30, 33)], 0.85);
        assert!(candidates.is_empty());
    }

    #[test]
    fn repeat_ad_rejects_unrelated_text() {
        let mut segments = repeat_ad_transcript();
        for seg in segments.iter_mut().filter(|s| s.sequence_num >= 30) {
            seg.text = "And now back to the show with our guest today.".to_string();
        }
        let candidates = find_repeat_ad_candidates(&segments, 10, 13, &[], 0.85);
        assert!(candidates.is_empty());
    }

    #[test]
    fn repeat_ad_ignores_trivially_short_ads() {
        let segments = vec![
            rseg(1, 0.0, "buy now"),
            rseg(2, 5.0, "unrelated"),
            rseg(3, 10.0, "buy now"),
        ];
        let candidates = find_repeat_ad_candidates(&segments, 1, 1, &[], 0.85);
        assert!(candidates.is_empty());
    }
}
