use std::error::Error;

use tokio::{net::TcpListener, signal};
use tracing::info;
use tracing_subscriber::EnvFilter;
use web_capability::{
    build_app, config::Settings, production_state_with_providers_and_fetch_config,
};

#[tokio::main]
async fn main() -> Result<(), Box<dyn Error>> {
    // 开发环境从 web-capability/.env 读取；生产环境仍可直接注入环境变量。
    let _ = dotenvy::from_filename(concat!(env!("CARGO_MANIFEST_DIR"), "/.env"));
    tracing_subscriber::fmt()
        .with_env_filter(
            // 默认只输出本服务日志，避免正文解析依赖在正常页面上产生大量告警噪声。
            EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| EnvFilter::new("web_capability=info")),
        )
        .init();

    let settings = Settings::from_env()?;
    let provider = settings.search_provider.name();
    let provider_configs =
        std::iter::once(settings.search_provider.clone()).chain(settings.search_fallbacks.clone());
    let state = production_state_with_providers_and_fetch_config(
        provider_configs,
        settings.allow_benchmark_dns_proxy,
        settings.search_policy.clone(),
        settings.robots.clone(),
    )?;
    let search_configured = state.search_service.configured();
    let listener = TcpListener::bind(settings.bind).await?;

    info!(
        bind = %settings.bind,
        provider,
        search_configured,
        benchmark_dns_proxy_enabled = settings.allow_benchmark_dns_proxy,
        robots_enabled = settings.robots.enabled,
        "Web Capability Service started"
    );
    axum::serve(listener, build_app(state))
        .with_graceful_shutdown(shutdown_signal())
        .await?;
    Ok(())
}

async fn shutdown_signal() {
    if let Err(error) = signal::ctrl_c().await {
        tracing::warn!(%error, "failed to install Ctrl+C handler");
    }
}
