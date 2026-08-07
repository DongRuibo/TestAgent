use std::{
    collections::HashMap,
    sync::Arc,
    time::{Duration, Instant},
};

use tokio::sync::Mutex;
use url::Url;

use crate::error::AppError;

/// RFC 9309 requires a crawler's product token to contain only these characters.
pub const ROBOTS_USER_AGENT: &str = "TestAgent-WebCapability";
pub const DEFAULT_ROBOTS_CACHE_TTL: Duration = Duration::from_secs(60 * 60);
pub const MAX_ROBOTS_CACHE_TTL: Duration = Duration::from_secs(24 * 60 * 60);
pub const MIN_ROBOTS_BODY_BYTES: usize = 500 * 1024;
pub const DEFAULT_ROBOTS_BODY_BYTES: usize = 512 * 1024;

#[derive(Debug, Clone)]
pub struct RobotsConfig {
    pub enabled: bool,
    pub cache_ttl: Duration,
    pub max_body_bytes: usize,
}

impl Default for RobotsConfig {
    fn default() -> Self {
        Self {
            enabled: true,
            cache_ttl: DEFAULT_ROBOTS_CACHE_TTL,
            max_body_bytes: DEFAULT_ROBOTS_BODY_BYTES,
        }
    }
}

impl RobotsConfig {
    pub fn validate(&self) -> Result<(), AppError> {
        if self.cache_ttl.is_zero() || self.cache_ttl > MAX_ROBOTS_CACHE_TTL {
            return Err(AppError::configuration(
                "WEB_FETCH_ROBOTS_CACHE_TTL_SECONDS 必须在 1 到 86400 之间",
            ));
        }
        if self.max_body_bytes < MIN_ROBOTS_BODY_BYTES {
            return Err(AppError::configuration(format!(
                "robots.txt 响应上限不能小于 {} KiB",
                MIN_ROBOTS_BODY_BYTES / 1024
            )));
        }
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct OriginKey {
    scheme: String,
    host: String,
    port: u16,
}

impl OriginKey {
    pub fn from_url(url: &Url) -> Result<Self, AppError> {
        let port = url
            .port_or_known_default()
            .ok_or_else(|| AppError::invalid_request("URL 端口无效"))?;
        let host = url
            .host_str()
            .ok_or_else(|| AppError::invalid_request("URL 缺少主机名"))?
            .trim_end_matches('.')
            .trim_start_matches('[')
            .trim_end_matches(']')
            .to_ascii_lowercase();
        Ok(Self {
            scheme: url.scheme().to_ascii_lowercase(),
            host,
            port,
        })
    }

    pub fn robots_url(&self) -> Result<Url, AppError> {
        let host = if self.host.contains(':') {
            format!("[{}]", self.host)
        } else {
            self.host.clone()
        };
        let authority = if (self.scheme == "http" && self.port == 80)
            || (self.scheme == "https" && self.port == 443)
        {
            host
        } else {
            format!("{host}:{}", self.port)
        };
        Url::parse(&format!("{}://{authority}/robots.txt", self.scheme))
            .map_err(|_| AppError::invalid_request("无法构造 robots.txt 地址"))
    }
}

#[derive(Debug, Clone)]
pub struct RobotsDecision {
    pub robots_url: String,
    pub cache_hit: bool,
}

#[derive(Debug, Clone)]
struct CachedPolicy {
    stored_at: Instant,
    policy: RobotsPolicy,
}

#[derive(Debug, Default, Clone)]
pub struct RobotsChecker {
    cache: Arc<Mutex<HashMap<OriginKey, CachedPolicy>>>,
}

impl RobotsChecker {
    pub async fn cached_policy(&self, origin: &OriginKey, ttl: Duration) -> Option<RobotsPolicy> {
        let mut cache = self.cache.lock().await;
        let entry = cache.get(origin)?;
        if entry.stored_at.elapsed() > ttl {
            cache.remove(origin);
            return None;
        }
        Some(entry.policy.clone())
    }

    pub async fn store_policy(&self, origin: OriginKey, policy: RobotsPolicy) {
        self.cache.lock().await.insert(
            origin,
            CachedPolicy {
                stored_at: Instant::now(),
                policy,
            },
        );
    }
}

#[derive(Debug, Clone, Default)]
pub struct RobotsPolicy {
    groups: Vec<RobotsGroup>,
}

#[derive(Debug, Clone, Default)]
struct RobotsGroup {
    agents: Vec<String>,
    rules: Vec<RobotsRule>,
}

#[derive(Debug, Clone)]
struct RobotsRule {
    allow: bool,
    pattern: String,
}

impl RobotsPolicy {
    pub fn allow_all() -> Self {
        Self::default()
    }

    pub fn parse(body: &[u8]) -> Result<Self, AppError> {
        let text = std::str::from_utf8(body)
            .map_err(|_| AppError::robots_parse("robots.txt 不是有效的 UTF-8 文本"))?;
        let mut groups = Vec::new();
        let mut current: Option<RobotsGroup> = None;
        let mut has_rule = false;

        for raw_line in text.lines() {
            let line = raw_line.split('#').next().unwrap_or_default().trim();
            if line.is_empty() {
                continue;
            }
            let Some((name, value)) = line.split_once(':') else {
                continue;
            };
            let name = name.trim().to_ascii_lowercase();
            let value = value.trim();
            match name.as_str() {
                "user-agent" => {
                    if has_rule {
                        if let Some(group) = current.take() {
                            groups.push(group);
                        }
                        has_rule = false;
                    }
                    let group = current.get_or_insert_with(RobotsGroup::default);
                    if !value.is_empty() {
                        group.agents.push(value.to_ascii_lowercase());
                    }
                }
                "allow" | "disallow" => {
                    let Some(group) = current.as_mut() else {
                        continue;
                    };
                    if name == "disallow" && value.is_empty() {
                        // An empty Disallow record is explicitly an allow-all rule.
                        has_rule = true;
                        continue;
                    }
                    group.rules.push(RobotsRule {
                        allow: name == "allow",
                        pattern: value.to_owned(),
                    });
                    has_rule = true;
                }
                // Sitemap and extension records do not alter group boundaries.
                _ => {}
            }
        }
        if let Some(group) = current {
            groups.push(group);
        }
        Ok(Self { groups })
    }

    pub fn is_allowed(&self, url: &Url) -> bool {
        let candidate = path_with_query(url);
        let mut matching_groups = self
            .groups
            .iter()
            .filter(|group| group.agents.iter().any(|agent| matches_user_agent(agent)))
            .collect::<Vec<_>>();
        if matching_groups.is_empty() {
            matching_groups = self
                .groups
                .iter()
                .filter(|group| group.agents.iter().any(|agent| agent == "*"))
                .collect();
        }

        let mut best: Option<(usize, bool)> = None;
        for group in matching_groups {
            for rule in &group.rules {
                if rule.pattern.is_empty() || pattern_matches(&rule.pattern, &candidate) {
                    let specificity = rule.pattern.trim_end_matches('$').len();
                    if best.is_none_or(|(length, allow)| {
                        specificity > length || (specificity == length && rule.allow && !allow)
                    }) {
                        best = Some((specificity, rule.allow));
                    }
                }
            }
        }
        best.is_none_or(|(_, allowed)| allowed)
    }
}

fn matches_user_agent(agent: &str) -> bool {
    ROBOTS_USER_AGENT
        .to_ascii_lowercase()
        .contains(&agent.to_ascii_lowercase())
}

fn path_with_query(url: &Url) -> String {
    let mut value = url.path().to_owned();
    if value.is_empty() {
        value.push('/');
    }
    if let Some(query) = url.query() {
        value.push('?');
        value.push_str(query);
    }
    value
}

fn pattern_matches(pattern: &str, candidate: &str) -> bool {
    let (pattern, end_anchor) = pattern
        .strip_suffix('$')
        .map_or((pattern, false), |value| (value, true));
    if pattern.is_empty() {
        return true;
    }
    let mut pieces = pattern.split('*');
    let first = pieces.next().unwrap_or_default();
    if !candidate.starts_with(first) {
        return false;
    }
    let mut offset = first.len();
    let remaining = pieces.collect::<Vec<_>>();
    if remaining.is_empty() {
        return !end_anchor || offset == candidate.len();
    }
    for (index, piece) in remaining.iter().enumerate() {
        if index == remaining.len() - 1 && end_anchor {
            return candidate[offset..].ends_with(piece)
                && candidate[offset..].len() >= piece.len();
        }
        let Some(found) = candidate[offset..].find(piece) else {
            return false;
        };
        offset += found + piece.len();
    }
    true
}

#[cfg(test)]
mod tests {
    use super::*;

    fn url(value: &str) -> Url {
        Url::parse(value).unwrap()
    }

    #[test]
    fn builds_origin_robots_url_without_query_or_fragment() {
        let origin = OriginKey::from_url(&url("https://Example.COM/a?q=1#frag")).unwrap();
        assert_eq!(
            origin.robots_url().unwrap().as_str(),
            "https://example.com/robots.txt"
        );
    }

    #[test]
    fn brackets_ipv6_origin_in_robots_url() {
        let origin = OriginKey::from_url(&url("https://[2001:4860:4860::8888]/")).unwrap();
        assert_eq!(
            origin.robots_url().unwrap().as_str(),
            "https://[2001:4860:4860::8888]/robots.txt"
        );
    }

    #[test]
    fn product_group_takes_precedence_over_wildcard() {
        let policy = RobotsPolicy::parse(
            b"User-agent: *\nDisallow: /private\n\nUser-agent: testagent-webcapability\nDisallow: /private\nAllow: /private/public\n",
        )
        .unwrap();
        assert!(policy.is_allowed(&url("https://example.com/private/public")));
        assert!(!policy.is_allowed(&url("https://example.com/private/secret")));
    }

    #[test]
    fn longest_match_and_allow_tie_are_applied() {
        let policy = RobotsPolicy::parse(
            b"User-agent: *\nDisallow: /a\nAllow: /a/b\nDisallow: /a/b/c*\nAllow: /a/b/c/public\n",
        )
        .unwrap();
        assert!(policy.is_allowed(&url("https://example.com/a/b")));
        assert!(policy.is_allowed(&url("https://example.com/a/b/c/public")));
    }

    #[test]
    fn supports_wildcards_end_anchor_and_empty_disallow() {
        let policy = RobotsPolicy::parse(
            b"User-agent: TestAgent-WebCapability\nDisallow: /tmp/*$\nDisallow:\n",
        )
        .unwrap();
        assert!(!policy.is_allowed(&url("https://example.com/tmp/a")));
        assert!(policy.is_allowed(&url("https://example.com/public")));
    }

    #[test]
    fn malformed_lines_and_comments_do_not_break_parse() {
        let policy = RobotsPolicy::parse(
            b"not a rule\nUser-agent: * # crawler\nDisallow: /secret # private\nSitemap: https://example.com/sitemap.xml\n",
        )
        .unwrap();
        assert!(!policy.is_allowed(&url("https://example.com/secret")));
        assert!(policy.is_allowed(&url("https://example.com/")));
    }
}
