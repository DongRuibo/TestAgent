use std::{collections::BTreeSet, time::Duration};

use crate::{
    error::AppError,
    extractor::{ExtractedArticle, extract_html, extract_plain_text},
    fetcher::{
        robots::{
            OriginKey, ROBOTS_USER_AGENT, RobotsChecker, RobotsConfig, RobotsDecision, RobotsPolicy,
        },
        url_guard::{ValidatedTarget, parse_and_validate_url, validate_and_resolve},
    },
    models::FetchedPage,
};
use chrono::Utc;
use reqwest::{
    Client, Response,
    header::{ACCEPT, ACCEPT_ENCODING, CONTENT_ENCODING, CONTENT_TYPE, HeaderMap, LOCATION},
    redirect::Policy,
};
use sha2::{Digest, Sha256};
use url::Url;

const CONNECT_TIMEOUT: Duration = Duration::from_secs(3);
const REQUEST_TIMEOUT: Duration = Duration::from_secs(15);
const MAX_REDIRECTS: usize = 5;
const MAX_BODY_BYTES: usize = 2 * 1024 * 1024;
const MAX_TEXT_CHARS: usize = 30_000;

#[derive(Debug, Default)]
pub struct HttpFetcher {
    allow_benchmark_dns_proxy: bool,
    robots_config: RobotsConfig,
    robots_checker: RobotsChecker,
}

impl HttpFetcher {
    pub fn new(allow_benchmark_dns_proxy: bool) -> Self {
        Self::new_with_robots_config(allow_benchmark_dns_proxy, RobotsConfig::default())
    }

    pub fn new_with_robots_config(
        allow_benchmark_dns_proxy: bool,
        robots_config: RobotsConfig,
    ) -> Self {
        Self {
            allow_benchmark_dns_proxy,
            robots_config,
            robots_checker: RobotsChecker::default(),
        }
    }

    pub fn benchmark_dns_proxy_enabled(&self) -> bool {
        self.allow_benchmark_dns_proxy
    }

    pub async fn fetch(&self, input: &str) -> Result<FetchedPage, AppError> {
        let requested = parse_and_validate_url(input)?;
        let requested_url = requested.to_string();
        let mut current = requested;
        let mut redirects = Vec::new();
        let mut resolved_addresses = BTreeSet::new();
        let mut robots_urls = Vec::new();
        let mut robots_cache_hits = 0;
        let mut robots_checked = false;

        loop {
            let target = validate_and_resolve(current, self.allow_benchmark_dns_proxy).await?;
            for address in &target.socket_addresses {
                resolved_addresses.insert(address.ip().to_string());
            }

            if self.robots_config.enabled {
                let decision = self.check_robots(&target).await?;
                robots_checked = true;
                robots_urls.push(decision.robots_url.clone());
                robots_cache_hits += usize::from(decision.cache_hit);
                tracing::info!(
                    url = %target.url,
                    robots_url = %decision.robots_url,
                    cache_hit = decision.cache_hit,
                    "robots.txt authorization passed"
                );
            }

            let mut response = send_pinned_request(&target).await?;
            if is_followable_redirect(response.status()) {
                if redirects.len() >= MAX_REDIRECTS {
                    return Err(AppError::too_many_redirects());
                }
                let location = response
                    .headers()
                    .get(LOCATION)
                    .and_then(|value| value.to_str().ok())
                    .ok_or_else(|| AppError::fetch("目标站点返回了无效的重定向地址"))?;
                let next = target
                    .url
                    .join(location)
                    .map_err(|_| AppError::fetch("目标站点返回了无效的重定向地址"))?;
                validate_redirect(&target.url, &next)?;

                // 下一轮会重新校验 URL、重新解析 DNS，并把连接固定到本轮通过校验的 IP。
                redirects.push(next.to_string());
                current = next;
                continue;
            }

            if !response.status().is_success() {
                return Err(AppError::upstream_http(response.status()));
            }

            validate_content_encoding(response.headers())?;

            let status_code = response.status().as_u16();
            let content_type = response
                .headers()
                .get(CONTENT_TYPE)
                .and_then(|value| value.to_str().ok())
                .map(str::trim)
                .map(str::to_owned);
            let media_type = content_type
                .as_deref()
                .and_then(|value| value.split(';').next())
                .map(str::trim)
                .map(str::to_ascii_lowercase);
            if !matches!(media_type.as_deref(), Some("text/html" | "text/plain")) {
                return Err(AppError::unsupported_content_type(content_type.as_deref()));
            }
            if response
                .content_length()
                .is_some_and(|length| length > MAX_BODY_BYTES as u64)
            {
                return Err(AppError::response_too_large());
            }

            let body = read_bounded_body(&mut response).await?;
            let decoded = String::from_utf8_lossy(&body);
            let final_url = target.url.to_string();
            let article = match media_type.as_deref() {
                Some("text/html") => extract_html(&decoded, &final_url)?,
                Some("text/plain") => extract_plain_text(&decoded)?,
                _ => unreachable!("content type was validated above"),
            };

            let body_hash = sha256_hex(&body);
            let content_hash = sha256_hex(article.text.as_bytes());
            let full_text_chars = article.text.chars().count();
            let (text, text_truncated) = truncate_chars(&article.text, MAX_TEXT_CHARS);

            return Ok(build_page(
                requested_url,
                final_url,
                status_code,
                content_type.expect("content type was validated above"),
                article,
                text,
                text_truncated,
                full_text_chars,
                body_hash,
                content_hash,
                redirects,
                robots_checked,
                robots_urls,
                robots_cache_hits,
                resolved_addresses.into_iter().collect(),
                dns_rebinding_protection_label(self.allow_benchmark_dns_proxy),
            ));
        }
    }

    async fn check_robots(&self, target: &ValidatedTarget) -> Result<RobotsDecision, AppError> {
        let origin = OriginKey::from_url(&target.url)?;
        let robots_url = origin.robots_url()?;
        if let Some(policy) = self
            .robots_checker
            .cached_policy(&origin, self.robots_config.cache_ttl)
            .await
        {
            if !policy.is_allowed(&target.url) {
                return Err(AppError::robots_denied(target.url.as_str()));
            }
            return Ok(RobotsDecision {
                robots_url: robots_url.to_string(),
                cache_hit: true,
            });
        }

        let policy = self.fetch_robots_policy(robots_url.clone()).await?;
        let allowed = policy.is_allowed(&target.url);
        self.robots_checker.store_policy(origin, policy).await;
        if !allowed {
            return Err(AppError::robots_denied(target.url.as_str()));
        }
        Ok(RobotsDecision {
            robots_url: robots_url.to_string(),
            cache_hit: false,
        })
    }

    async fn fetch_robots_policy(&self, initial_url: url::Url) -> Result<RobotsPolicy, AppError> {
        let mut current = initial_url;
        let mut redirects = 0;
        loop {
            let target = validate_and_resolve(current, self.allow_benchmark_dns_proxy)
                .await
                .map_err(|_| AppError::robots_unreachable("robots.txt 地址未通过安全校验"))?;
            let mut response = send_pinned_robots_request(&target).await?;

            if is_followable_redirect(response.status()) {
                if redirects >= MAX_REDIRECTS {
                    return Err(AppError::robots_unreachable(
                        "robots.txt 重定向超过 5 次限制",
                    ));
                }
                let location = response
                    .headers()
                    .get(LOCATION)
                    .and_then(|value| value.to_str().ok())
                    .ok_or_else(|| {
                        AppError::robots_unreachable("robots.txt 返回了无效的重定向地址")
                    })?;
                let next = target.url.join(location).map_err(|_| {
                    AppError::robots_unreachable("robots.txt 返回了无效的重定向地址")
                })?;
                validate_redirect(&target.url, &next)
                    .map_err(|_| AppError::robots_unreachable("robots.txt 重定向未通过安全校验"))?;
                redirects += 1;
                current = next;
                continue;
            }

            if response.status().is_client_error() {
                // RFC 9309 将 4xx 视为 robots 文件不可用，允许继续抓取。
                return Ok(RobotsPolicy::allow_all());
            }
            if response.status().is_server_error() {
                return Err(AppError::robots_unreachable(format!(
                    "robots.txt 返回 HTTP {}，按安全策略停止抓取",
                    response.status().as_u16()
                )));
            }
            if !response.status().is_success() {
                return Err(AppError::robots_unreachable(format!(
                    "robots.txt 返回 HTTP {}",
                    response.status().as_u16()
                )));
            }

            validate_content_encoding(response.headers())
                .map_err(|_| AppError::robots_parse("robots.txt 使用了不支持的响应编码"))?;
            if response
                .content_length()
                .is_some_and(|length| length > self.robots_config.max_body_bytes as u64)
            {
                return Err(AppError::robots_response_too_large());
            }
            let body = read_bounded_body_with_limit(
                &mut response,
                self.robots_config.max_body_bytes,
                AppError::robots_response_too_large,
            )
            .await?;
            return RobotsPolicy::parse(&body);
        }
    }
}

async fn send_pinned_request(target: &ValidatedTarget) -> Result<Response, AppError> {
    let mut builder = Client::builder()
        .connect_timeout(CONNECT_TIMEOUT)
        .timeout(REQUEST_TIMEOUT)
        .redirect(Policy::none())
        // 代理会绕过本地 DNS 固定，因此抓取客户端明确禁用环境代理。
        .no_proxy()
        .user_agent("TestAgent-WebCapability/0.1")
        .pool_max_idle_per_host(0);
    if let Some(host) = &target.dns_override_host {
        builder = builder.resolve_to_addrs(host, &target.socket_addresses);
    }
    let client = builder
        .build()
        .map_err(|_| AppError::fetch("无法初始化安全抓取客户端"))?;

    client
        .get(target.url.clone())
        .header(ACCEPT, "text/html, text/plain;q=0.9")
        .header(ACCEPT_ENCODING, "identity")
        .send()
        .await
        .map_err(|_| AppError::fetch("网页请求失败"))
}

async fn send_pinned_robots_request(target: &ValidatedTarget) -> Result<Response, AppError> {
    let mut builder = Client::builder()
        .connect_timeout(CONNECT_TIMEOUT)
        .timeout(REQUEST_TIMEOUT)
        .redirect(Policy::none())
        .no_proxy()
        .user_agent(ROBOTS_USER_AGENT)
        .pool_max_idle_per_host(0);
    if let Some(host) = &target.dns_override_host {
        builder = builder.resolve_to_addrs(host, &target.socket_addresses);
    }
    let client = builder
        .build()
        .map_err(|_| AppError::robots_unreachable("无法初始化 robots.txt 抓取客户端"))?;

    client
        .get(target.url.clone())
        .header(ACCEPT, "text/plain")
        .header(ACCEPT_ENCODING, "identity")
        .send()
        .await
        .map_err(|_| AppError::robots_unreachable("robots.txt 请求失败"))
}

async fn read_bounded_body(response: &mut Response) -> Result<Vec<u8>, AppError> {
    read_bounded_body_with_limit(response, MAX_BODY_BYTES, AppError::response_too_large).await
}

async fn read_bounded_body_with_limit(
    response: &mut Response,
    max_body_bytes: usize,
    too_large: fn() -> AppError,
) -> Result<Vec<u8>, AppError> {
    let mut body = Vec::with_capacity(
        response
            .content_length()
            .unwrap_or(0)
            .min(max_body_bytes as u64) as usize,
    );
    while let Some(chunk) = response
        .chunk()
        .await
        .map_err(|_| AppError::fetch("读取网页响应失败"))?
    {
        if body.len().saturating_add(chunk.len()) > max_body_bytes {
            return Err(too_large());
        }
        body.extend_from_slice(&chunk);
    }
    Ok(body)
}

fn is_followable_redirect(status: reqwest::StatusCode) -> bool {
    matches!(status.as_u16(), 301 | 302 | 303 | 307 | 308)
}

fn validate_redirect(previous: &Url, next: &Url) -> Result<(), AppError> {
    if previous.scheme() == "https" && next.scheme() == "http" {
        return Err(AppError::url_blocked("不允许从 HTTPS 降级重定向到 HTTP"));
    }
    Ok(())
}

fn validate_content_encoding(headers: &HeaderMap) -> Result<(), AppError> {
    for value in headers.get_all(CONTENT_ENCODING) {
        let value = value
            .to_str()
            .map_err(|_| AppError::unsupported_content_encoding("invalid header encoding"))?;
        if value
            .split(',')
            .map(str::trim)
            .any(|coding| !coding.eq_ignore_ascii_case("identity"))
        {
            return Err(AppError::unsupported_content_encoding(value));
        }
    }
    Ok(())
}

#[allow(clippy::too_many_arguments)]
fn build_page(
    requested_url: String,
    final_url: String,
    status_code: u16,
    content_type: String,
    article: ExtractedArticle,
    text: String,
    text_truncated: bool,
    full_text_chars: usize,
    body_hash: String,
    content_hash: String,
    redirects: Vec<String>,
    robots_checked: bool,
    robots_urls: Vec<String>,
    robots_cache_hits: usize,
    resolved_addresses: Vec<String>,
    dns_rebinding_protection: &'static str,
) -> FetchedPage {
    FetchedPage {
        requested_url,
        final_url,
        status_code,
        content_type,
        title: article.title,
        text,
        author: article.author,
        published_at: article.published_at,
        modified_at: article.modified_at,
        fetch_method: "http",
        retrieved_at: Utc::now(),
        body_hash,
        content_hash,
        text_truncated,
        extraction_quality: (full_text_chars as f32 / 1_000.0).min(1.0),
        redirects,
        robots_checked,
        robots_urls,
        robots_cache_hits,
        resolved_addresses,
        dns_rebinding_protection,
        trust: "untrusted",
        instructions_executable: false,
    }
}

fn dns_rebinding_protection_label(allow_benchmark_dns_proxy: bool) -> &'static str {
    if allow_benchmark_dns_proxy {
        "ip_pinned_per_hop_with_benchmark_dns_proxy_exception"
    } else {
        "ip_pinned_per_hop"
    }
}

fn sha256_hex(value: &[u8]) -> String {
    let digest = Sha256::digest(value);
    digest.iter().map(|byte| format!("{byte:02x}")).collect()
}

fn truncate_chars(value: &str, max_chars: usize) -> (String, bool) {
    let mut chars = value.chars();
    let output: String = chars.by_ref().take(max_chars).collect();
    let truncated = chars.next().is_some();
    (output, truncated)
}

#[cfg(test)]
mod tests {
    use reqwest::header::HeaderValue;

    use super::*;

    #[test]
    fn truncates_by_unicode_characters_not_bytes() {
        let (value, truncated) = truncate_chars("你好吗", 2);
        assert_eq!(value, "你好");
        assert!(truncated);
    }

    #[test]
    fn produces_stable_sha256() {
        assert_eq!(
            sha256_hex(b"abc"),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        );
    }

    #[test]
    fn rejects_https_to_http_redirect_downgrade() {
        let https = Url::parse("https://example.com/start").unwrap();
        let http = Url::parse("http://example.com/end").unwrap();
        assert_eq!(
            validate_redirect(&https, &http).unwrap_err().error_type(),
            "URL_BLOCKED"
        );
        assert!(validate_redirect(&http, &https).is_ok());
    }

    #[test]
    fn accepts_only_identity_content_encoding() {
        let mut headers = HeaderMap::new();
        assert!(validate_content_encoding(&headers).is_ok());
        headers.insert(CONTENT_ENCODING, HeaderValue::from_static("identity"));
        assert!(validate_content_encoding(&headers).is_ok());
        headers.insert(CONTENT_ENCODING, HeaderValue::from_static("gzip"));
        assert_eq!(
            validate_content_encoding(&headers)
                .unwrap_err()
                .error_type(),
            "UNSUPPORTED_CONTENT_ENCODING"
        );
    }

    #[test]
    fn audit_label_discloses_benchmark_dns_proxy_exception() {
        assert_eq!(dns_rebinding_protection_label(false), "ip_pinned_per_hop");
        assert_eq!(
            dns_rebinding_protection_label(true),
            "ip_pinned_per_hop_with_benchmark_dns_proxy_exception"
        );
    }
}
