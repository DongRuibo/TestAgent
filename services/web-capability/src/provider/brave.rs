use std::time::Duration;

use async_trait::async_trait;
use reqwest::{
    Client,
    header::{ACCEPT, HeaderValue},
    redirect::Policy,
};
use serde::Deserialize;
use url::Url;

use crate::{
    error::AppError,
    models::SearchResult,
    provider::SearchProvider,
    ranking::{RawSearchResult, normalize_and_deduplicate},
};

const BRAVE_SEARCH_ENDPOINT: &str = "https://api.search.brave.com/res/v1/web/search";
const MAX_PROVIDER_BODY_BYTES: usize = 1024 * 1024;

pub struct BraveSearchProvider {
    client: Client,
    api_key: Option<HeaderValue>,
}

impl BraveSearchProvider {
    pub fn new(api_key: Option<String>) -> Result<Self, AppError> {
        let client = Client::builder()
            .connect_timeout(Duration::from_secs(3))
            .timeout(Duration::from_secs(10))
            .redirect(Policy::none())
            // 固定官方端点直接连接，避免环境代理接触服务端密钥。
            .no_proxy()
            .user_agent("TestAgent-WebCapability/0.1")
            .pool_max_idle_per_host(10)
            .build()
            .map_err(|_| AppError::configuration("无法初始化 Brave Search HTTP 客户端"))?;

        let api_key = api_key
            .map(|value| {
                let mut header = HeaderValue::from_str(value.trim())
                    .map_err(|_| AppError::configuration("BRAVE_SEARCH_API_KEY 包含无效字符"))?;
                header.set_sensitive(true);
                Ok(header)
            })
            .transpose()?;

        Ok(Self { client, api_key })
    }
}

#[derive(Debug, Deserialize)]
struct BraveResponse {
    web: Option<BraveWeb>,
}

#[derive(Debug, Deserialize)]
struct BraveWeb {
    #[serde(default)]
    results: Vec<BraveResult>,
}

#[derive(Debug, Deserialize)]
struct BraveResult {
    title: String,
    url: String,
    description: Option<String>,
    #[serde(default)]
    extra_snippets: Vec<String>,
    #[serde(default)]
    page_age: Option<String>,
    #[serde(default)]
    age: Option<String>,
}

#[async_trait]
impl SearchProvider for BraveSearchProvider {
    fn name(&self) -> &'static str {
        "brave"
    }

    fn configured(&self) -> bool {
        self.api_key.is_some()
    }

    async fn search(&self, query: &str, max_results: usize) -> Result<Vec<SearchResult>, AppError> {
        let api_key = self.api_key.as_ref().ok_or_else(|| {
            AppError::configuration("尚未配置 BRAVE_SEARCH_API_KEY；fetch 可用，但 search 暂不可用")
        })?;

        let url = build_search_url(query, max_results)?;

        let mut response = self
            .client
            .get(url)
            .header("X-Subscription-Token", api_key)
            .header(ACCEPT, "application/json")
            .send()
            .await
            .map_err(|_| AppError::search_provider("Brave Search 请求失败", true))?;

        if !response.status().is_success() {
            let status = response.status();
            let retryable = status.as_u16() == 429 || status.is_server_error();
            return Err(AppError::search_provider(
                format!("Brave Search 返回 HTTP {}", status.as_u16()),
                retryable,
            ));
        }

        if response
            .content_length()
            .is_some_and(|length| length > MAX_PROVIDER_BODY_BYTES as u64)
        {
            return Err(AppError::search_provider(
                "Brave Search 响应超过 1 MiB 限制",
                false,
            ));
        }

        let mut body = Vec::new();
        while let Some(chunk) = response
            .chunk()
            .await
            .map_err(|_| AppError::search_provider("读取 Brave Search 响应失败", true))?
        {
            if body.len().saturating_add(chunk.len()) > MAX_PROVIDER_BODY_BYTES {
                return Err(AppError::search_provider(
                    "Brave Search 响应超过 1 MiB 限制",
                    false,
                ));
            }
            body.extend_from_slice(&chunk);
        }

        let parsed: BraveResponse = serde_json::from_slice(&body)
            .map_err(|_| AppError::search_provider("Brave Search 响应格式无效", true))?;

        let raw = parsed
            .web
            .map(|web| web.results)
            .unwrap_or_default()
            .into_iter()
            .map(|item| RawSearchResult {
                title: item.title,
                url: item.url,
                snippet: item.description,
                extra_snippets: item.extra_snippets,
                published_at: item.page_age.or(item.age),
                provider_score: None,
            });

        // 仅在内存中规范化并返回，不持久化 Brave 原始结果或响应。
        Ok(normalize_and_deduplicate(raw, self.name(), max_results))
    }
}

fn build_search_url(query: &str, max_results: usize) -> Result<Url, AppError> {
    let mut url = Url::parse(BRAVE_SEARCH_ENDPOINT)
        .map_err(|_| AppError::configuration("Brave Search 固定端点配置无效"))?;
    {
        let mut pairs = url.query_pairs_mut();
        pairs.append_pair("q", query);
        pairs.append_pair("count", &max_results.to_string());
        pairs.append_pair("result_filter", "web");
        pairs.append_pair("text_decorations", "false");
        pairs.append_pair("extra_snippets", "true");
        pairs.append_pair("safesearch", "moderate");
    }
    Ok(url)
}

#[cfg(test)]
mod tests {
    use std::collections::HashMap;

    use super::*;

    #[test]
    fn brave_url_uses_only_the_web_result_filter_and_plain_text() {
        let url = build_search_url("rust agent", 5).unwrap();
        let params: HashMap<_, _> = url.query_pairs().into_owned().collect();
        assert_eq!(url.as_str().split('?').next(), Some(BRAVE_SEARCH_ENDPOINT));
        assert_eq!(params.get("result_filter").map(String::as_str), Some("web"));
        assert_eq!(
            params.get("text_decorations").map(String::as_str),
            Some("false")
        );
        assert_eq!(params.get("count").map(String::as_str), Some("5"));
    }
}
