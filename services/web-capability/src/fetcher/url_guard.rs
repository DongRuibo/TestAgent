use std::{
    collections::HashSet,
    net::{IpAddr, Ipv4Addr, Ipv6Addr, SocketAddr},
    time::Duration,
};

use tokio::{net::lookup_host, time::timeout};
use url::{Host, Url};

use crate::error::AppError;

const DNS_TIMEOUT: Duration = Duration::from_secs(3);

#[derive(Debug)]
pub struct ValidatedTarget {
    pub url: Url,
    pub dns_override_host: Option<String>,
    pub socket_addresses: Vec<SocketAddr>,
}

pub fn parse_and_validate_url(input: &str) -> Result<Url, AppError> {
    let url = Url::parse(input.trim())
        .map_err(|_| AppError::invalid_request("url 必须是有效的绝对 URL"))?;

    validate_url(&url)?;
    Ok(url)
}

pub fn validate_url(url: &Url) -> Result<(), AppError> {
    if !matches!(url.scheme(), "http" | "https") {
        return Err(AppError::url_blocked("只允许 http 或 https URL"));
    }
    if !url.username().is_empty() || url.password().is_some() {
        return Err(AppError::url_blocked("URL 不允许包含用户名或密码"));
    }
    let host = url
        .host_str()
        .ok_or_else(|| AppError::invalid_request("URL 缺少主机名"))?;
    let normalized_host = host.trim_end_matches('.').to_ascii_lowercase();
    if normalized_host.is_empty() {
        return Err(AppError::invalid_request("URL 缺少主机名"));
    }
    if is_blocked_hostname(&normalized_host) {
        return Err(AppError::url_blocked("不允许访问本机或云元数据主机"));
    }
    let port = url
        .port_or_known_default()
        .ok_or_else(|| AppError::invalid_request("URL 端口无效"))?;
    if !matches!(port, 80 | 443) {
        return Err(AppError::url_blocked("MVP 仅允许访问 80 或 443 端口"));
    }

    Ok(())
}

pub async fn validate_and_resolve(
    url: Url,
    allow_benchmark_dns_proxy: bool,
) -> Result<ValidatedTarget, AppError> {
    // 重定向后的每一跳也必须重新执行完整 URL policy，不能只复查 DNS/IP。
    validate_url(&url)?;
    let port = url
        .port_or_known_default()
        .ok_or_else(|| AppError::invalid_request("URL 端口无效"))?;
    let host = url
        .host()
        .ok_or_else(|| AppError::invalid_request("URL 缺少主机名"))?;

    let (dns_override_host, candidates) = match host {
        Host::Ipv4(address) => (None, vec![SocketAddr::new(address.into(), port)]),
        Host::Ipv6(address) => (None, vec![SocketAddr::new(address.into(), port)]),
        Host::Domain(domain) => {
            let domain = domain.to_owned();
            let resolved = timeout(DNS_TIMEOUT, lookup_host((domain.as_str(), port)))
                .await
                .map_err(|_| AppError::dns_resolution("DNS 解析超时"))?
                .map_err(|_| AppError::dns_resolution("DNS 解析失败"))?;
            let addresses = resolved.collect();
            (Some(domain), addresses)
        }
    };

    let mut seen = HashSet::new();
    let mut socket_addresses = Vec::new();
    let is_domain_dns_answer = dns_override_host.is_some();
    for address in candidates {
        if !is_allowed_resolved_ip(
            address.ip(),
            is_domain_dns_answer,
            allow_benchmark_dns_proxy,
        ) {
            // 任一 DNS 答案不安全就拒绝整个请求，避免客户端挑中内网答案。
            return Err(AppError::url_blocked(
                "目标主机解析到了非公网、保留或元数据网络地址",
            ));
        }
        if seen.insert(address) {
            socket_addresses.push(address);
        }
    }

    if socket_addresses.is_empty() {
        return Err(AppError::dns_resolution("目标主机没有可用的 DNS 地址"));
    }

    Ok(ValidatedTarget {
        url,
        dns_override_host,
        socket_addresses,
    })
}

fn is_allowed_resolved_ip(
    address: IpAddr,
    is_domain_dns_answer: bool,
    allow_benchmark_dns_proxy: bool,
) -> bool {
    is_global_ip(address)
        || (allow_benchmark_dns_proxy && is_domain_dns_answer && is_benchmark_ipv4(address))
}

fn is_benchmark_ipv4(address: IpAddr) -> bool {
    let IpAddr::V4(address) = address else {
        return false;
    };
    let [a, b, _, _] = address.octets();
    a == 198 && matches!(b, 18 | 19)
}

fn is_blocked_hostname(host: &str) -> bool {
    host == "localhost"
        || host.ends_with(".localhost")
        || matches!(
            host,
            "metadata"
                | "instance-data"
                | "metadata.google.internal"
                | "metadata.goog"
                | "metadata.aws.internal"
                | "metadata.azure.internal"
        )
}

pub fn is_global_ip(address: IpAddr) -> bool {
    match address {
        IpAddr::V4(address) => is_global_ipv4(address),
        IpAddr::V6(address) => is_global_ipv6(address),
    }
}

fn is_global_ipv4(address: Ipv4Addr) -> bool {
    let [a, b, c, _] = address.octets();

    if address == Ipv4Addr::new(168, 63, 129, 16) {
        return false; // Azure 平台虚拟地址，不能作为普通公网抓取目标。
    }

    !matches!(
        (a, b, c),
        (0, _, _)
            | (10, _, _)
            | (100, 64..=127, _)
            | (127, _, _)
            | (169, 254, _)
            | (172, 16..=31, _)
            | (192, 0, 0)
            | (192, 0, 2)
            | (192, 88, 99)
            | (192, 168, _)
            | (198, 18..=19, _)
            | (198, 51, 100)
            | (203, 0, 113)
            | (224..=255, _, _)
    )
}

fn is_global_ipv6(address: Ipv6Addr) -> bool {
    let segments = address.segments();

    // 仅允许当前全球单播范围 2000::/3，并排除其中的特殊用途前缀。
    if segments[0] & 0xe000 != 0x2000 {
        return false;
    }
    if segments[0] == 0x2001 && segments[1] <= 0x01ff {
        return false; // IETF 协议分配、Teredo、benchmark、ORCHID 等。
    }
    if segments[0] == 0x2001 && segments[1] == 0x0db8 {
        return false; // 文档地址。
    }
    if segments[0] == 0x2002 {
        return false; // 6to4 可嵌入非公网 IPv4。
    }
    if segments[0] == 0x3fff && segments[1] & 0xf000 == 0 {
        return false; // 3fff:0::/20 文档地址。
    }

    true
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn blocks_local_credentials_and_metadata_urls() {
        for input in [
            "file:///etc/passwd",
            "http://user:pass@example.com/",
            "http://localhost/",
            "http://metadata.google.internal/latest/meta-data/",
        ] {
            assert!(parse_and_validate_url(input).is_err(), "{input}");
        }
    }

    #[test]
    fn allows_only_web_ports() {
        assert!(parse_and_validate_url("http://example.com/").is_ok());
        assert!(parse_and_validate_url("https://example.com/").is_ok());
        assert!(parse_and_validate_url("https://example.com:80/").is_ok());
        let error = parse_and_validate_url("https://example.com:8443/").unwrap_err();
        assert_eq!(error.error_type(), "URL_BLOCKED");
    }

    #[test]
    fn every_parsed_redirect_target_uses_the_same_url_policy() {
        let base = Url::parse("https://example.com/start").unwrap();
        let alternate_port = base.join("https://example.org:8443/next").unwrap();
        let credentials = base.join("https://user:pass@example.org/next").unwrap();
        assert_eq!(
            validate_url(&alternate_port).unwrap_err().error_type(),
            "URL_BLOCKED"
        );
        assert_eq!(
            validate_url(&credentials).unwrap_err().error_type(),
            "URL_BLOCKED"
        );
    }

    #[test]
    fn url_parser_canonicalizes_integer_ipv4_before_guarding() {
        let url = parse_and_validate_url("http://2130706433/").unwrap();
        assert_eq!(url.host_str(), Some("127.0.0.1"));
        assert!(!is_global_ip(url.host_str().unwrap().parse().unwrap()));
    }

    #[test]
    fn classifies_ipv4_networks() {
        for address in [
            "0.0.0.1",
            "10.0.0.1",
            "100.64.0.1",
            "127.0.0.1",
            "168.63.129.16",
            "169.254.169.254",
            "172.16.0.1",
            "192.168.1.1",
            "198.18.0.1",
            "203.0.113.1",
            "224.0.0.1",
            "255.255.255.255",
        ] {
            assert!(!is_global_ip(address.parse().unwrap()), "{address}");
        }
        assert!(is_global_ip("8.8.8.8".parse().unwrap()));
    }

    #[test]
    fn benchmark_proxy_exception_requires_domain_dns_and_opt_in() {
        let benchmark = "198.18.0.1".parse().unwrap();
        assert!(!is_allowed_resolved_ip(benchmark, true, false));
        assert!(is_allowed_resolved_ip(benchmark, true, true));
        assert!(!is_allowed_resolved_ip(benchmark, false, true));

        let private = "10.0.0.1".parse().unwrap();
        assert!(!is_allowed_resolved_ip(private, true, true));
        assert!(is_allowed_resolved_ip(
            "8.8.8.8".parse().unwrap(),
            true,
            false
        ));
    }

    #[test]
    fn classifies_ipv6_networks() {
        for address in [
            "::",
            "::1",
            "::ffff:127.0.0.1",
            "fc00::1",
            "fe80::1",
            "ff02::1",
            "2001:db8::1",
            "2002:7f00:1::",
            "3fff::1",
        ] {
            assert!(!is_global_ip(address.parse().unwrap()), "{address}");
        }
        assert!(is_global_ip("2001:4860:4860::8888".parse().unwrap()));
    }
}
