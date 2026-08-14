use async_trait::async_trait;

#[derive(Debug, Clone)]
pub struct SearchUsageEvent {
    pub request_id: String,
    pub tenant_id: Option<String>,
    pub agent_id: Option<String>,
    pub provider: Option<String>,
    pub elapsed_ms: u64,
    pub max_results: usize,
    pub result_count: usize,
    pub cache_hit: bool,
    pub attempts: usize,
    pub status: &'static str,
    pub error_type: Option<String>,
}

#[async_trait]
pub trait UsageLogger: Send + Sync {
    async fn log(&self, event: SearchUsageEvent);
}

#[derive(Debug, Default)]
pub struct TracingUsageLogger;

#[async_trait]
impl UsageLogger for TracingUsageLogger {
    async fn log(&self, event: SearchUsageEvent) {
        tracing::info!(
            target: "web_capability::usage",
            event = "web_search_usage",
            request_id = %event.request_id,
            tenant_id = event.tenant_id.as_deref().unwrap_or(""),
            agent_id = event.agent_id.as_deref().unwrap_or(""),
            provider = event.provider.as_deref().unwrap_or(""),
            elapsed_ms = event.elapsed_ms,
            max_results = event.max_results,
            result_count = event.result_count,
            cache_hit = event.cache_hit,
            attempts = event.attempts,
            status = event.status,
            error_type = event.error_type.as_deref().unwrap_or(""),
            "Web search usage recorded"
        );
    }
}
