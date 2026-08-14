use axum::{
    Json,
    http::StatusCode,
    response::{IntoResponse, Response},
};
use serde::Serialize;
use thiserror::Error;

#[derive(Debug, Error)]
#[error("{message}")]
pub struct AppError {
    status: StatusCode,
    error_type: &'static str,
    message: String,
    retryable: bool,
    retry_after_seconds: Option<u64>,
    attempts: Option<usize>,
    request_id: Option<String>,
}

#[derive(Debug, Serialize)]
struct ErrorEnvelope<'a> {
    success: bool,
    error_type: &'a str,
    message: &'a str,
    retryable: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    retry_after_seconds: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    attempts: Option<usize>,
    #[serde(skip_serializing_if = "Option::is_none")]
    request_id: Option<&'a str>,
}

impl AppError {
    pub fn new(
        status: StatusCode,
        error_type: &'static str,
        message: impl Into<String>,
        retryable: bool,
    ) -> Self {
        Self {
            status,
            error_type,
            message: message.into(),
            retryable,
            retry_after_seconds: None,
            attempts: None,
            request_id: None,
        }
    }

    pub fn invalid_request(message: impl Into<String>) -> Self {
        Self::new(StatusCode::BAD_REQUEST, "INVALID_REQUEST", message, false)
    }

    pub fn configuration(message: impl Into<String>) -> Self {
        Self::new(
            StatusCode::SERVICE_UNAVAILABLE,
            "CONFIGURATION_ERROR",
            message,
            false,
        )
    }

    pub fn url_blocked(message: impl Into<String>) -> Self {
        Self::new(StatusCode::BAD_REQUEST, "URL_BLOCKED", message, false)
    }

    pub fn dns_resolution(message: impl Into<String>) -> Self {
        Self::new(
            StatusCode::BAD_GATEWAY,
            "DNS_RESOLUTION_ERROR",
            message,
            true,
        )
    }

    pub fn fetch(message: impl Into<String>) -> Self {
        Self::new(StatusCode::BAD_GATEWAY, "FETCH_ERROR", message, true)
    }

    pub fn upstream_http(status: StatusCode) -> Self {
        let retryable = status == StatusCode::TOO_MANY_REQUESTS || status.is_server_error();
        Self::new(
            StatusCode::BAD_GATEWAY,
            "UPSTREAM_HTTP_ERROR",
            format!("目标站点返回 HTTP {}", status.as_u16()),
            retryable,
        )
    }

    pub fn unsupported_content_type(content_type: Option<&str>) -> Self {
        let value = content_type.unwrap_or("missing");
        Self::new(
            StatusCode::UNSUPPORTED_MEDIA_TYPE,
            "UNSUPPORTED_CONTENT_TYPE",
            format!("只允许 text/html 或 text/plain，实际 Content-Type: {value}"),
            false,
        )
    }

    pub fn unsupported_content_encoding(content_encoding: &str) -> Self {
        Self::new(
            StatusCode::UNSUPPORTED_MEDIA_TYPE,
            "UNSUPPORTED_CONTENT_ENCODING",
            format!("只允许 identity 响应编码，实际 Content-Encoding: {content_encoding}"),
            false,
        )
    }

    pub fn response_too_large() -> Self {
        Self::new(
            StatusCode::PAYLOAD_TOO_LARGE,
            "RESPONSE_TOO_LARGE",
            "网页响应超过 2 MiB 限制",
            false,
        )
    }

    pub fn too_many_redirects() -> Self {
        Self::new(
            StatusCode::BAD_GATEWAY,
            "TOO_MANY_REDIRECTS",
            "网页重定向超过 5 次限制",
            false,
        )
    }

    pub fn robots_denied(url: &str) -> Self {
        Self::new(
            StatusCode::FORBIDDEN,
            "ROBOTS_DENIED",
            format!("robots.txt 不允许抓取该 URL: {url}"),
            false,
        )
    }

    pub fn robots_unreachable(message: impl Into<String>) -> Self {
        Self::new(StatusCode::BAD_GATEWAY, "ROBOTS_UNREACHABLE", message, true)
    }

    pub fn robots_parse(message: impl Into<String>) -> Self {
        Self::new(
            StatusCode::BAD_GATEWAY,
            "ROBOTS_PARSE_ERROR",
            message,
            false,
        )
    }

    pub fn robots_response_too_large() -> Self {
        Self::new(
            StatusCode::PAYLOAD_TOO_LARGE,
            "ROBOTS_RESPONSE_TOO_LARGE",
            "robots.txt 响应超过 512 KiB 限制",
            false,
        )
    }

    pub fn extraction(message: impl Into<String>) -> Self {
        Self::new(
            StatusCode::UNPROCESSABLE_ENTITY,
            "EXTRACTION_ERROR",
            message,
            false,
        )
    }

    pub fn search_provider(message: impl Into<String>, retryable: bool) -> Self {
        Self::new(
            StatusCode::BAD_GATEWAY,
            "SEARCH_PROVIDER_ERROR",
            message,
            retryable,
        )
    }

    pub fn with_retry_after_seconds(mut self, retry_after_seconds: Option<u64>) -> Self {
        self.retry_after_seconds = retry_after_seconds;
        self
    }

    pub fn with_attempts(mut self, attempts: usize) -> Self {
        self.attempts = Some(attempts);
        self
    }

    pub fn with_request_id(mut self, request_id: impl Into<String>) -> Self {
        self.request_id = Some(request_id.into());
        self
    }

    pub fn retryable(&self) -> bool {
        self.retryable
    }

    pub fn retry_after_seconds(&self) -> Option<u64> {
        self.retry_after_seconds
    }

    pub fn attempts(&self) -> Option<usize> {
        self.attempts
    }

    pub fn error_type(&self) -> &'static str {
        self.error_type
    }

    pub fn not_found() -> Self {
        Self::new(StatusCode::NOT_FOUND, "NOT_FOUND", "接口不存在", false)
    }

    pub fn method_not_allowed() -> Self {
        Self::new(
            StatusCode::METHOD_NOT_ALLOWED,
            "METHOD_NOT_ALLOWED",
            "该接口不支持此 HTTP 方法",
            false,
        )
    }
}

impl IntoResponse for AppError {
    fn into_response(self) -> Response {
        let body = ErrorEnvelope {
            success: false,
            error_type: self.error_type,
            message: &self.message,
            retryable: self.retryable,
            retry_after_seconds: self.retry_after_seconds,
            attempts: self.attempts,
            request_id: self.request_id.as_deref(),
        };
        (self.status, Json(body)).into_response()
    }
}

#[cfg(test)]
mod tests {
    use axum::body::to_bytes;

    use super::*;

    #[tokio::test]
    async fn retry_after_is_serialized_only_when_present() {
        let response = AppError::search_provider("limited", true)
            .with_retry_after_seconds(Some(17))
            .into_response();
        let body = to_bytes(response.into_body(), 4096).await.unwrap();
        let body: serde_json::Value = serde_json::from_slice(&body).unwrap();

        assert_eq!(body["retryable"], true);
        assert_eq!(body["retry_after_seconds"], 17);

        let response = AppError::search_provider("failed", false).into_response();
        let body = to_bytes(response.into_body(), 4096).await.unwrap();
        let body: serde_json::Value = serde_json::from_slice(&body).unwrap();
        assert!(body.get("retry_after_seconds").is_none());
    }
}
