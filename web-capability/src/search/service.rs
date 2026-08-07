use std::{
    collections::HashMap,
    sync::{
        Arc,
        atomic::{AtomicU64, Ordering},
    },
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

use axum::http::HeaderMap;
use tokio::{
    sync::Mutex,
    time::{sleep, timeout},
};

use crate::{
    error::AppError,
    models::{MAX_RESULTS, SearchResult},
    provider::SearchProvider,
};

use super::{
    CachedSearch, MemorySearchCache, SearchCache, SearchUsageEvent, TracingUsageLogger, UsageLogger,
};

const MAX_QUERY_CHARS: usize = 400;
const MAX_QUERY_WORDS: usize = 50;
const MAX_CONTEXT_VALUE_CHARS: usize = 128;
static REQUEST_SEQUENCE: AtomicU64 = AtomicU64::new(1);

#[derive(Debug, Clone)]
pub struct SearchPolicy {
    pub total_timeout: Duration,
    pub attempt_timeout: Duration,
    pub max_attempts: usize,
    pub retry_base_delay: Duration,
    pub cache_enabled: bool,
    pub cache_ttl: Duration,
    pub cache_max_entries: usize,
}

impl Default for SearchPolicy {
    fn default() -> Self {
        Self {
            total_timeout: Duration::from_secs(10),
            attempt_timeout: Duration::from_secs(4),
            max_attempts: 3,
            retry_base_delay: Duration::from_millis(200),
            cache_enabled: true,
            cache_ttl: Duration::from_secs(600),
            cache_max_entries: 1_000,
        }
    }
}

impl SearchPolicy {
    pub fn validate(&self) -> Result<(), AppError> {
        if self.total_timeout.is_zero()
            || self.attempt_timeout.is_zero()
            || self.attempt_timeout > self.total_timeout
            || self.max_attempts == 0
            || self.cache_ttl.is_zero()
            || self.cache_max_entries == 0
        {
            return Err(AppError::configuration("搜索策略参数无效"));
        }
        Ok(())
    }
}

#[derive(Debug, Clone)]
pub struct SearchContext {
    pub request_id: String,
    pub tenant_id: Option<String>,
    pub agent_id: Option<String>,
}

impl SearchContext {
    pub fn generated() -> Self {
        Self {
            request_id: next_request_id(),
            tenant_id: None,
            agent_id: None,
        }
    }

    pub fn from_headers(headers: &HeaderMap) -> Result<Self, AppError> {
        let request_id = header_value(headers, "x-request-id")?.unwrap_or_else(next_request_id);
        Ok(Self {
            request_id,
            tenant_id: header_value(headers, "x-tenant-id")?,
            agent_id: header_value(headers, "x-agent-id")?,
        })
    }
}

#[derive(Debug, Clone)]
pub struct SearchQuery {
    pub query: String,
    pub max_results: usize,
}

impl SearchQuery {
    pub fn new(query: &str, max_results: usize) -> Result<Self, AppError> {
        let query = normalize_query(query);
        if query.is_empty() {
            return Err(AppError::invalid_request("query 不能为空"));
        }
        if query.chars().count() > MAX_QUERY_CHARS {
            return Err(AppError::invalid_request("query 不能超过 400 个字符"));
        }
        if query.split_whitespace().count() > MAX_QUERY_WORDS {
            return Err(AppError::invalid_request("query 不能超过 50 个词"));
        }
        if !(1..=MAX_RESULTS).contains(&max_results) {
            return Err(AppError::invalid_request("max_results 必须在 1 到 20 之间"));
        }
        Ok(Self { query, max_results })
    }
}

#[derive(Debug, Clone)]
pub struct SearchOutcome {
    pub query: String,
    pub provider: String,
    pub results: Vec<SearchResult>,
    pub request_id: String,
    pub cache_hit: bool,
    pub attempts: usize,
    pub elapsed_ms: u64,
}

struct UsageRecord<'a> {
    max_results: usize,
    provider: Option<&'a str>,
    attempts: usize,
    result_count: usize,
    cache_hit: bool,
    status: &'static str,
    error: Option<&'a AppError>,
}

pub struct SearchService {
    providers: Vec<Arc<dyn SearchProvider>>,
    cache: Arc<dyn SearchCache>,
    usage_logger: Arc<dyn UsageLogger>,
    policy: SearchPolicy,
    singleflight: Mutex<HashMap<String, Arc<Mutex<()>>>>,
}

impl SearchService {
    pub fn from_provider(provider: Arc<dyn SearchProvider>) -> Self {
        let policy = SearchPolicy::default();
        let cache = MemorySearchCache::new(policy.cache_ttl, policy.cache_max_entries);
        Self {
            providers: vec![provider],
            cache,
            usage_logger: Arc::new(TracingUsageLogger),
            policy,
            singleflight: Mutex::new(HashMap::new()),
        }
    }

    pub fn new(
        providers: Vec<Arc<dyn SearchProvider>>,
        policy: SearchPolicy,
        cache: Option<Arc<dyn SearchCache>>,
        usage_logger: Option<Arc<dyn UsageLogger>>,
    ) -> Result<Self, AppError> {
        if providers.is_empty() {
            return Err(AppError::configuration("至少需要一个搜索 Provider"));
        }
        policy.validate()?;
        Ok(Self {
            providers,
            cache: cache.unwrap_or_else(|| {
                MemorySearchCache::new(policy.cache_ttl, policy.cache_max_entries)
            }),
            usage_logger: usage_logger.unwrap_or_else(|| Arc::new(TracingUsageLogger)),
            policy,
            singleflight: Mutex::new(HashMap::new()),
        })
    }

    pub fn configured(&self) -> bool {
        self.providers.iter().any(|provider| provider.configured())
    }

    pub fn provider_names(&self) -> Vec<&'static str> {
        self.providers
            .iter()
            .map(|provider| provider.name())
            .collect()
    }

    pub async fn search(
        &self,
        query: &str,
        max_results: usize,
        context: SearchContext,
    ) -> Result<SearchOutcome, AppError> {
        let started = Instant::now();
        let request_id = context.request_id.clone();
        let parsed = match SearchQuery::new(query, max_results) {
            Ok(parsed) => parsed,
            Err(error) => {
                let error = error.with_request_id(request_id.clone());
                self.record(
                    &context,
                    started,
                    UsageRecord {
                        max_results,
                        provider: None,
                        attempts: 0,
                        result_count: 0,
                        cache_hit: false,
                        status: "invalid",
                        error: Some(&error),
                    },
                )
                .await;
                return Err(error);
            }
        };
        let key = self.cache_key(&parsed, &context);

        if self.policy.cache_enabled
            && let Some(cached) = self.cache.get(&key).await
        {
            let elapsed_ms = elapsed_ms(started);
            self.record(
                &context,
                started,
                UsageRecord {
                    max_results: parsed.max_results,
                    provider: Some(&cached.provider),
                    attempts: 0,
                    result_count: cached.results.len(),
                    cache_hit: true,
                    status: "cache_hit",
                    error: None,
                },
            )
            .await;
            return Ok(SearchOutcome {
                query: parsed.query,
                provider: cached.provider,
                results: cached.results,
                request_id,
                cache_hit: true,
                attempts: 0,
                elapsed_ms,
            });
        }

        let _singleflight_guard = if self.policy.cache_enabled {
            let lock = self.singleflight_lock(&key).await;
            Some(lock.lock_owned().await)
        } else {
            None
        };

        if self.policy.cache_enabled
            && let Some(cached) = self.cache.get(&key).await
        {
            let elapsed_ms = elapsed_ms(started);
            self.record(
                &context,
                started,
                UsageRecord {
                    max_results: parsed.max_results,
                    provider: Some(&cached.provider),
                    attempts: 0,
                    result_count: cached.results.len(),
                    cache_hit: true,
                    status: "cache_hit",
                    error: None,
                },
            )
            .await;
            return Ok(SearchOutcome {
                query: parsed.query,
                provider: cached.provider,
                results: cached.results,
                request_id,
                cache_hit: true,
                attempts: 0,
                elapsed_ms,
            });
        }

        let search_result = self.search_providers(&parsed).await;
        let elapsed_ms = elapsed_ms(started);
        match search_result {
            Ok((provider, results, attempts)) => {
                if self.policy.cache_enabled {
                    self.cache
                        .put(
                            key,
                            CachedSearch {
                                provider: provider.clone(),
                                results: results.clone(),
                            },
                        )
                        .await;
                }
                self.record(
                    &context,
                    started,
                    UsageRecord {
                        max_results: parsed.max_results,
                        provider: Some(&provider),
                        attempts,
                        result_count: results.len(),
                        cache_hit: false,
                        status: "success",
                        error: None,
                    },
                )
                .await;
                Ok(SearchOutcome {
                    query: parsed.query,
                    provider,
                    results,
                    request_id,
                    cache_hit: false,
                    attempts,
                    elapsed_ms,
                })
            }
            Err((error, provider)) => {
                let error = error.with_request_id(request_id);
                let attempts = error_attempts(&error);
                self.record(
                    &context,
                    started,
                    UsageRecord {
                        max_results: parsed.max_results,
                        provider: provider.as_deref(),
                        attempts,
                        result_count: 0,
                        cache_hit: false,
                        status: "error",
                        error: Some(&error),
                    },
                )
                .await;
                Err(error)
            }
        }
    }

    async fn search_providers(
        &self,
        query: &SearchQuery,
    ) -> Result<(String, Vec<SearchResult>, usize), (AppError, Option<String>)> {
        let mut total_attempts = 0;
        let mut last_error = None;
        let mut last_provider = None;
        for provider in &self.providers {
            let result = self.search_provider_with_retry(provider, query).await;
            match result {
                Ok((results, attempts)) => {
                    total_attempts += attempts;
                    return Ok((provider.name().to_owned(), results, total_attempts));
                }
                Err((error, attempts)) => {
                    total_attempts += attempts;
                    let retryable = error.retryable();
                    last_error = Some(error);
                    last_provider = Some(provider.name().to_owned());
                    if !retryable {
                        break;
                    }
                }
            }
        }
        Err((
            last_error
                .unwrap_or_else(|| AppError::configuration("没有可用的搜索 Provider"))
                .with_attempts(total_attempts),
            last_provider,
        ))
    }

    async fn search_provider_with_retry(
        &self,
        provider: &Arc<dyn SearchProvider>,
        query: &SearchQuery,
    ) -> Result<(Vec<SearchResult>, usize), (AppError, usize)> {
        let deadline = Instant::now() + self.policy.total_timeout;
        let mut attempts = 0;
        while attempts < self.policy.max_attempts {
            let remaining = deadline.saturating_duration_since(Instant::now());
            if remaining.is_zero() {
                return Err((
                    AppError::search_provider("搜索总时间预算已耗尽", true),
                    attempts,
                ));
            }
            attempts += 1;
            let attempt_timeout = self.policy.attempt_timeout.min(remaining);
            let result = timeout(
                attempt_timeout,
                provider.search(&query.query, query.max_results),
            )
            .await;
            match result {
                Ok(Ok(results)) => return Ok((results, attempts)),
                Ok(Err(error)) if !error.retryable() || attempts >= self.policy.max_attempts => {
                    return Err((error, attempts));
                }
                Ok(Err(error)) => {
                    if !self
                        .wait_before_retry(error.retry_after_seconds(), attempts, deadline)
                        .await
                    {
                        return Err((error, attempts));
                    }
                }
                Err(_) if attempts >= self.policy.max_attempts => {
                    return Err((
                        AppError::search_provider("搜索 Provider 请求超时", true),
                        attempts,
                    ));
                }
                Err(_) => {
                    if !self.wait_before_retry(None, attempts, deadline).await {
                        return Err((
                            AppError::search_provider("搜索 Provider 请求超时", true),
                            attempts,
                        ));
                    }
                }
            }
        }
        Err((
            AppError::search_provider("搜索总时间预算已耗尽", true),
            attempts,
        ))
    }

    async fn wait_before_retry(
        &self,
        retry_after_seconds: Option<u64>,
        attempts: usize,
        deadline: Instant,
    ) -> bool {
        let remaining = deadline.saturating_duration_since(Instant::now());
        let exponential_ms = self
            .policy
            .retry_base_delay
            .as_millis()
            .saturating_mul(2_u128.saturating_pow((attempts.saturating_sub(1)) as u32));
        let jitter_ms = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|duration| u128::from(duration.subsec_millis() % 100))
            .unwrap_or(0);
        let backoff = Duration::from_millis(
            exponential_ms
                .saturating_add(jitter_ms)
                .min(u128::from(u64::MAX)) as u64,
        );
        let requested = retry_after_seconds
            .map(Duration::from_secs)
            .unwrap_or(backoff);
        if requested >= remaining {
            return false;
        }
        sleep(requested).await;
        true
    }

    fn cache_key(&self, query: &SearchQuery, context: &SearchContext) -> String {
        let tenant = context.tenant_id.as_deref().unwrap_or("anonymous");
        let providers = self
            .providers
            .iter()
            .map(|provider| provider.name())
            .collect::<Vec<_>>()
            .join(",");
        format!(
            "v1\0{tenant}\0{providers}\0{}\0{}",
            query.max_results, query.query
        )
    }

    async fn singleflight_lock(&self, key: &str) -> Arc<Mutex<()>> {
        let mut locks = self.singleflight.lock().await;
        if locks.len() >= self.policy.cache_max_entries.saturating_mul(4) {
            locks.clear();
        }
        locks
            .entry(key.to_owned())
            .or_insert_with(|| Arc::new(Mutex::new(())))
            .clone()
    }

    async fn record(&self, context: &SearchContext, started: Instant, input: UsageRecord<'_>) {
        self.usage_logger
            .log(SearchUsageEvent {
                request_id: context.request_id.clone(),
                tenant_id: context.tenant_id.clone(),
                agent_id: context.agent_id.clone(),
                provider: input.provider.map(str::to_owned),
                elapsed_ms: elapsed_ms(started),
                max_results: input.max_results,
                result_count: input.result_count,
                cache_hit: input.cache_hit,
                attempts: input.attempts,
                status: input.status,
                error_type: input.error.map(|error| error.error_type().to_owned()),
            })
            .await;
    }
}

fn normalize_query(query: &str) -> String {
    query.split_whitespace().collect::<Vec<_>>().join(" ")
}

fn elapsed_ms(started: Instant) -> u64 {
    started.elapsed().as_millis().min(u128::from(u64::MAX)) as u64
}

fn error_attempts(error: &AppError) -> usize {
    error.attempts().unwrap_or(0)
}

fn header_value(headers: &HeaderMap, name: &str) -> Result<Option<String>, AppError> {
    let Some(value) = headers.get(name) else {
        return Ok(None);
    };
    let value = value
        .to_str()
        .map_err(|_| AppError::invalid_request(format!("{name} 必须是有效的 ASCII 文本")))?
        .trim();
    if value.is_empty() {
        return Ok(None);
    }
    if value.chars().count() > MAX_CONTEXT_VALUE_CHARS {
        return Err(AppError::invalid_request(format!(
            "{name} 不能超过 {MAX_CONTEXT_VALUE_CHARS} 个字符"
        )));
    }
    Ok(Some(value.to_owned()))
}

fn next_request_id() -> String {
    let sequence = REQUEST_SEQUENCE.fetch_add(1, Ordering::Relaxed);
    let timestamp = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_millis())
        .unwrap_or(0);
    format!("search-{timestamp}-{sequence}")
}

#[cfg(test)]
mod tests {
    use std::sync::atomic::AtomicUsize;

    use async_trait::async_trait;

    use super::*;

    struct TestProvider {
        provider_name: &'static str,
        calls: Arc<AtomicUsize>,
        failures: usize,
    }

    #[async_trait]
    impl SearchProvider for TestProvider {
        fn name(&self) -> &'static str {
            self.provider_name
        }

        fn configured(&self) -> bool {
            true
        }

        async fn search(
            &self,
            _query: &str,
            _max_results: usize,
        ) -> Result<Vec<SearchResult>, AppError> {
            let call = self.calls.fetch_add(1, Ordering::SeqCst);
            if call < self.failures {
                return Err(AppError::search_provider("transient", true));
            }
            Ok(vec![SearchResult {
                provider: self.provider_name.into(),
                rank: 1,
                title: "Test result".into(),
                url: "https://example.com/result".into(),
                snippet: Some("snippet".into()),
                extra_snippets: vec![],
                published_at: None,
                domain: "example.com".into(),
                provider_score: None,
                rerank_score: None,
            }])
        }
    }

    #[derive(Default)]
    struct RecordingLogger {
        events: Mutex<Vec<SearchUsageEvent>>,
    }

    #[async_trait]
    impl UsageLogger for RecordingLogger {
        async fn log(&self, event: SearchUsageEvent) {
            self.events.lock().await.push(event);
        }
    }

    #[test]
    fn normalizes_query_without_lowercasing() {
        let query = SearchQuery::new("  Rust   Agent  ", 5).unwrap();
        assert_eq!(query.query, "Rust Agent");
    }

    #[test]
    fn rejects_invalid_policy() {
        let policy = SearchPolicy {
            max_attempts: 0,
            ..SearchPolicy::default()
        };
        assert_eq!(
            policy.validate().unwrap_err().error_type(),
            "CONFIGURATION_ERROR"
        );
    }

    #[tokio::test]
    async fn caches_successful_searches_and_records_cache_hit() {
        let calls = Arc::new(AtomicUsize::new(0));
        let logger = Arc::new(RecordingLogger::default());
        let service = SearchService::new(
            vec![Arc::new(TestProvider {
                provider_name: "mock",
                calls: calls.clone(),
                failures: 0,
            })],
            SearchPolicy {
                retry_base_delay: Duration::ZERO,
                ..SearchPolicy::default()
            },
            None,
            Some(logger.clone()),
        )
        .unwrap();

        let first = service
            .search(
                " Rust   Agent ",
                5,
                SearchContext {
                    request_id: "one".into(),
                    tenant_id: Some("tenant".into()),
                    agent_id: Some("agent-a".into()),
                },
            )
            .await
            .unwrap();
        let second = service
            .search(
                "Rust Agent",
                5,
                SearchContext {
                    request_id: "two".into(),
                    tenant_id: Some("tenant".into()),
                    agent_id: Some("agent-a".into()),
                },
            )
            .await
            .unwrap();

        assert!(!first.cache_hit);
        assert!(second.cache_hit);
        assert_eq!(calls.load(Ordering::SeqCst), 1);
        let events = logger.events.lock().await;
        assert_eq!(events.len(), 2);
        assert_eq!(events[1].status, "cache_hit");
    }

    #[tokio::test]
    async fn retries_transient_provider_failure_within_attempt_budget() {
        let calls = Arc::new(AtomicUsize::new(0));
        let service = SearchService::new(
            vec![Arc::new(TestProvider {
                provider_name: "mock",
                calls: calls.clone(),
                failures: 2,
            })],
            SearchPolicy {
                total_timeout: Duration::from_secs(1),
                attempt_timeout: Duration::from_millis(100),
                max_attempts: 3,
                retry_base_delay: Duration::ZERO,
                cache_enabled: false,
                ..SearchPolicy::default()
            },
            None,
            None,
        )
        .unwrap();

        let outcome = service
            .search("retry me", 5, SearchContext::generated())
            .await
            .unwrap();
        assert_eq!(outcome.attempts, 3);
        assert_eq!(calls.load(Ordering::SeqCst), 3);
    }

    #[tokio::test]
    async fn falls_back_to_next_provider_after_retryable_failure() {
        let first_calls = Arc::new(AtomicUsize::new(0));
        let second_calls = Arc::new(AtomicUsize::new(0));
        let service = SearchService::new(
            vec![
                Arc::new(TestProvider {
                    provider_name: "first",
                    calls: first_calls.clone(),
                    failures: 10,
                }),
                Arc::new(TestProvider {
                    provider_name: "second",
                    calls: second_calls.clone(),
                    failures: 0,
                }),
            ],
            SearchPolicy {
                total_timeout: Duration::from_secs(1),
                attempt_timeout: Duration::from_millis(100),
                max_attempts: 1,
                retry_base_delay: Duration::ZERO,
                cache_enabled: false,
                ..SearchPolicy::default()
            },
            None,
            None,
        )
        .unwrap();

        let outcome = service
            .search("fallback", 5, SearchContext::generated())
            .await
            .unwrap();
        assert_eq!(outcome.provider, "second");
        assert_eq!(outcome.attempts, 2);
        assert_eq!(first_calls.load(Ordering::SeqCst), 1);
        assert_eq!(second_calls.load(Ordering::SeqCst), 1);
    }
}
