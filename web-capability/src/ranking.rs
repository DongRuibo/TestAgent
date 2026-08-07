use std::collections::HashSet;

use url::Url;

use crate::models::SearchResult;

#[derive(Debug)]
pub struct RawSearchResult {
    pub title: String,
    pub url: String,
    pub snippet: Option<String>,
    pub extra_snippets: Vec<String>,
    pub published_at: Option<String>,
    pub provider_score: Option<f32>,
}

struct NormalizedUrl {
    output: String,
    dedupe_key: String,
    domain: String,
}

pub fn normalize_and_deduplicate(
    raw_results: impl IntoIterator<Item = RawSearchResult>,
    provider: &str,
    max_results: usize,
) -> Vec<SearchResult> {
    let mut seen = HashSet::new();
    let mut output = Vec::new();

    for raw in raw_results {
        let Some(url) = normalize_url(&raw.url) else {
            continue;
        };
        if !seen.insert(url.dedupe_key) {
            continue;
        }

        output.push(SearchResult {
            provider: provider.to_owned(),
            rank: output.len() + 1,
            title: cap_chars(raw.title.trim(), 500),
            url: url.output,
            snippet: raw
                .snippet
                .map(|value| cap_chars(value.trim(), 2_000))
                .filter(|value| !value.is_empty()),
            extra_snippets: raw
                .extra_snippets
                .into_iter()
                .take(5)
                .map(|value| cap_chars(value.trim(), 1_000))
                .filter(|value| !value.is_empty())
                .collect(),
            published_at: raw.published_at,
            domain: url.domain,
            provider_score: raw.provider_score,
            rerank_score: None,
        });

        if output.len() == max_results {
            break;
        }
    }

    output
}

fn normalize_url(input: &str) -> Option<NormalizedUrl> {
    let mut url = Url::parse(input).ok()?;
    if !matches!(url.scheme(), "http" | "https")
        || !url.username().is_empty()
        || url.password().is_some()
    {
        return None;
    }

    let domain = url.host_str()?.trim_end_matches('.').to_ascii_lowercase();
    if domain.is_empty() {
        return None;
    }

    url.set_fragment(None);
    if matches!(
        (url.scheme(), url.port()),
        ("http", Some(80)) | ("https", Some(443))
    ) {
        let _ = url.set_port(None);
    }

    if url.path().len() > 1 && url.path().ends_with('/') {
        let path = url.path().trim_end_matches('/').to_owned();
        url.set_path(&path);
    }

    let mut query_pairs: Vec<(String, String)> = url
        .query_pairs()
        .filter(|(key, _)| !is_tracking_parameter(key))
        .map(|(key, value)| (key.into_owned(), value.into_owned()))
        .collect();
    query_pairs.sort();

    url.set_query(None);
    if !query_pairs.is_empty() {
        let mut pairs = url.query_pairs_mut();
        for (key, value) in query_pairs {
            pairs.append_pair(&key, &value);
        }
    }

    let output = url.to_string();
    let mut dedupe_url = url;
    let _ = dedupe_url.set_scheme("https");

    Some(NormalizedUrl {
        output,
        dedupe_key: dedupe_url.to_string(),
        domain,
    })
}

fn is_tracking_parameter(key: &str) -> bool {
    let key = key.to_ascii_lowercase();
    key.starts_with("utm_")
        || matches!(
            key.as_str(),
            "fbclid" | "gclid" | "dclid" | "msclkid" | "mc_cid" | "mc_eid"
        )
}

fn cap_chars(value: &str, max_chars: usize) -> String {
    value.chars().take(max_chars).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn normalizes_and_deduplicates_tracking_variants() {
        let results = normalize_and_deduplicate(
            [
                RawSearchResult {
                    title: "First".into(),
                    url: "http://Example.COM/a/?utm_source=x&b=2&a=1#part".into(),
                    snippet: None,
                    extra_snippets: vec![],
                    published_at: None,
                    provider_score: None,
                },
                RawSearchResult {
                    title: "Duplicate".into(),
                    url: "https://example.com/a?a=1&b=2".into(),
                    snippet: None,
                    extra_snippets: vec![],
                    published_at: None,
                    provider_score: None,
                },
            ],
            "brave",
            5,
        );

        assert_eq!(results.len(), 1);
        assert_eq!(results[0].rank, 1);
        assert_eq!(results[0].url, "http://example.com/a?a=1&b=2");
    }

    #[test]
    fn drops_non_http_and_credential_urls() {
        let results = normalize_and_deduplicate(
            [
                RawSearchResult {
                    title: "File".into(),
                    url: "file:///etc/passwd".into(),
                    snippet: None,
                    extra_snippets: vec![],
                    published_at: None,
                    provider_score: None,
                },
                RawSearchResult {
                    title: "Credential".into(),
                    url: "https://user:pass@example.com/".into(),
                    snippet: None,
                    extra_snippets: vec![],
                    published_at: None,
                    provider_score: None,
                },
            ],
            "brave",
            5,
        );
        assert!(results.is_empty());
    }
}
