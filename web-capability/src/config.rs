use std::{env, net::SocketAddr};

use crate::{
    error::AppError,
    fetcher::robots::{DEFAULT_ROBOTS_BODY_BYTES, DEFAULT_ROBOTS_CACHE_TTL, RobotsConfig},
    provider::SearchProviderConfig,
    search::SearchPolicy,
};

pub const DEFAULT_BIND: &str = "127.0.0.1:3020";

#[derive(Clone)]
pub struct Settings {
    pub bind: SocketAddr,
    pub search_provider: SearchProviderConfig,
    pub search_fallbacks: Vec<SearchProviderConfig>,
    pub search_policy: SearchPolicy,
    pub allow_benchmark_dns_proxy: bool,
    pub robots: RobotsConfig,
}

impl Settings {
    pub fn from_env() -> Result<Self, AppError> {
        let bind_value = env::var("WEB_CAPABILITY_BIND").unwrap_or_else(|_| DEFAULT_BIND.into());
        let bind = bind_value.parse::<SocketAddr>().map_err(|_| {
            AppError::configuration(format!(
                "WEB_CAPABILITY_BIND 不是有效的监听地址: {bind_value}"
            ))
        })?;

        let brave_search_api_key = optional_env("BRAVE_SEARCH_API_KEY");
        let tavily_search_api_key = optional_env("TAVILY_API_KEY")
            .or_else(|| optional_env("TAVILY_SEARCH_API_KEY"))
            // 兼容已有开发配置；新配置统一使用 Tavily 官方变量名。
            .or_else(|| optional_env("Tavily_SEARCH_API_KEY"));
        let exa_search_api_key = optional_env("EXA_API_KEY");
        let primary_name = optional_env("WEB_SEARCH_PRIMARY_PROVIDER")
            .or_else(|| optional_env("WEB_SEARCH_PROVIDER"));
        let search_provider = select_search_provider_extended(
            primary_name.as_deref(),
            brave_search_api_key.clone(),
            tavily_search_api_key.clone(),
            exa_search_api_key.clone(),
        )?;
        let search_fallbacks = parse_fallbacks(
            optional_env("WEB_SEARCH_FALLBACK_PROVIDERS").as_deref(),
            search_provider.name(),
            brave_search_api_key,
            tavily_search_api_key,
            exa_search_api_key,
        )?;
        let search_policy = search_policy_from_env()?;
        let allow_benchmark_dns_proxy = env::var("WEB_CAPABILITY_ALLOW_BENCHMARK_DNS_PROXY")
            .ok()
            .map(|value| parse_bool("WEB_CAPABILITY_ALLOW_BENCHMARK_DNS_PROXY", &value))
            .transpose()?
            .unwrap_or(false);
        let robots = RobotsConfig {
            enabled: env_bool("WEB_FETCH_ROBOTS_ENABLED", true)?,
            cache_ttl: std::time::Duration::from_secs(env_u64(
                "WEB_FETCH_ROBOTS_CACHE_TTL_SECONDS",
                DEFAULT_ROBOTS_CACHE_TTL.as_secs(),
            )?),
            max_body_bytes: env_usize(
                "WEB_FETCH_ROBOTS_MAX_BODY_BYTES",
                DEFAULT_ROBOTS_BODY_BYTES,
            )?,
        };
        robots.validate()?;

        Ok(Self {
            bind,
            search_provider,
            search_fallbacks,
            search_policy,
            allow_benchmark_dns_proxy,
            robots,
        })
    }
}

fn select_search_provider_extended(
    explicit_provider: Option<&str>,
    brave_api_key: Option<String>,
    tavily_api_key: Option<String>,
    exa_api_key: Option<String>,
) -> Result<SearchProviderConfig, AppError> {
    let provider = explicit_provider
        .map(str::trim)
        .filter(|value| !value.is_empty());
    match provider.map(str::to_ascii_lowercase).as_deref() {
        Some("brave") => Ok(SearchProviderConfig::Brave {
            api_key: brave_api_key,
        }),
        Some("tavily") => Ok(SearchProviderConfig::Tavily {
            api_key: tavily_api_key,
        }),
        Some("exa") => Ok(SearchProviderConfig::Exa {
            api_key: exa_api_key,
        }),
        Some(value) => Err(AppError::configuration(format!(
            "搜索 Provider 不支持 {value}；可选值为 brave、tavily 或 exa"
        ))),
        None if tavily_api_key.is_some() => Ok(SearchProviderConfig::Tavily {
            api_key: tavily_api_key,
        }),
        None if brave_api_key.is_some() => Ok(SearchProviderConfig::Brave {
            api_key: brave_api_key,
        }),
        None if exa_api_key.is_some() => Ok(SearchProviderConfig::Exa {
            api_key: exa_api_key,
        }),
        // 保持原有无 Key 时的 Brave 默认行为；服务仍可启动供 web_fetch 使用。
        None => Ok(SearchProviderConfig::Brave {
            api_key: brave_api_key,
        }),
    }
}

fn parse_fallbacks(
    configured: Option<&str>,
    primary: &str,
    brave_api_key: Option<String>,
    tavily_api_key: Option<String>,
    exa_api_key: Option<String>,
) -> Result<Vec<SearchProviderConfig>, AppError> {
    let Some(configured) = configured else {
        return Ok(Vec::new());
    };
    let mut fallbacks = Vec::new();
    for raw_name in configured.split(',') {
        let name = raw_name.trim().to_ascii_lowercase();
        if name.is_empty() || name == primary {
            continue;
        }
        let config = match name.as_str() {
            "brave" => SearchProviderConfig::Brave {
                api_key: brave_api_key.clone(),
            },
            "tavily" => SearchProviderConfig::Tavily {
                api_key: tavily_api_key.clone(),
            },
            "exa" => SearchProviderConfig::Exa {
                api_key: exa_api_key.clone(),
            },
            _ => {
                return Err(AppError::configuration(format!(
                    "WEB_SEARCH_FALLBACK_PROVIDERS 不支持 {name}"
                )));
            }
        };
        if !fallbacks
            .iter()
            .any(|existing: &SearchProviderConfig| existing.name() == config.name())
        {
            fallbacks.push(config);
        }
    }
    Ok(fallbacks)
}

fn search_policy_from_env() -> Result<SearchPolicy, AppError> {
    let defaults = SearchPolicy::default();
    let policy = SearchPolicy {
        total_timeout: std::time::Duration::from_millis(env_u64(
            "WEB_SEARCH_TOTAL_TIMEOUT_MS",
            defaults.total_timeout.as_millis() as u64,
        )?),
        attempt_timeout: std::time::Duration::from_millis(env_u64(
            "WEB_SEARCH_ATTEMPT_TIMEOUT_MS",
            defaults.attempt_timeout.as_millis() as u64,
        )?),
        max_attempts: env_usize("WEB_SEARCH_MAX_ATTEMPTS", defaults.max_attempts)?,
        retry_base_delay: std::time::Duration::from_millis(env_u64(
            "WEB_SEARCH_RETRY_BASE_DELAY_MS",
            defaults.retry_base_delay.as_millis() as u64,
        )?),
        cache_enabled: env_bool("WEB_SEARCH_CACHE_ENABLED", defaults.cache_enabled)?,
        cache_ttl: std::time::Duration::from_secs(env_u64(
            "WEB_SEARCH_CACHE_TTL_SECONDS",
            defaults.cache_ttl.as_secs(),
        )?),
        cache_max_entries: env_usize("WEB_SEARCH_CACHE_MAX_ENTRIES", defaults.cache_max_entries)?,
    };
    policy.validate()?;
    Ok(policy)
}

fn env_u64(name: &str, default: u64) -> Result<u64, AppError> {
    match env::var(name) {
        Ok(value) => value
            .trim()
            .parse()
            .map_err(|_| AppError::configuration(format!("{name} 必须是非负整数"))),
        Err(_) => Ok(default),
    }
}

fn env_usize(name: &str, default: usize) -> Result<usize, AppError> {
    match env::var(name) {
        Ok(value) => value
            .trim()
            .parse()
            .map_err(|_| AppError::configuration(format!("{name} 必须是非负整数"))),
        Err(_) => Ok(default),
    }
}

fn env_bool(name: &str, default: bool) -> Result<bool, AppError> {
    match env::var(name) {
        Ok(value) => parse_bool(name, &value),
        Err(_) => Ok(default),
    }
}

fn optional_env(name: &str) -> Option<String> {
    env::var(name)
        .ok()
        .map(|value| value.trim().to_owned())
        .filter(|value| !value.is_empty())
}

fn parse_bool(name: &str, value: &str) -> Result<bool, AppError> {
    match value.trim().to_ascii_lowercase().as_str() {
        "true" => Ok(true),
        "false" => Ok(false),
        _ => Err(AppError::configuration(format!(
            "{name} 只能配置为 true 或 false"
        ))),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn benchmark_proxy_opt_in_is_strict_boolean() {
        assert!(parse_bool("FLAG", "true").unwrap());
        assert!(!parse_bool("FLAG", " FALSE ").unwrap());
        assert_eq!(
            parse_bool("FLAG", "1").unwrap_err().error_type(),
            "CONFIGURATION_ERROR"
        );
    }

    #[test]
    fn tavily_key_selects_tavily_when_provider_is_not_explicit() {
        let config =
            select_search_provider_extended(None, None, Some("secret".into()), None).unwrap();
        assert_eq!(config.name(), "tavily");
    }

    #[test]
    fn explicit_provider_wins_and_unknown_provider_is_rejected() {
        let config = select_search_provider_extended(
            Some(" BRAVE "),
            Some("brave-secret".into()),
            Some("tavily-secret".into()),
            None,
        )
        .unwrap();
        assert_eq!(config.name(), "brave");

        let error = match select_search_provider_extended(Some("unknown"), None, None, None) {
            Ok(_) => panic!("unknown provider should be rejected"),
            Err(error) => error,
        };
        assert_eq!(error.error_type(), "CONFIGURATION_ERROR");
    }

    #[test]
    fn exa_and_explicit_fallbacks_are_supported() {
        let config =
            select_search_provider_extended(Some("exa"), None, None, Some("exa-secret".into()))
                .unwrap();
        assert_eq!(config.name(), "exa");
        let fallbacks = parse_fallbacks(
            Some("brave, exa, brave"),
            "tavily",
            Some("brave-secret".into()),
            Some("tavily-secret".into()),
            Some("exa-secret".into()),
        )
        .unwrap();
        assert_eq!(
            fallbacks.iter().map(|item| item.name()).collect::<Vec<_>>(),
            vec!["brave", "exa"]
        );
    }
}
