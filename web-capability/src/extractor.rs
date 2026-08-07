use dom_smoothie::{Config, Readability, TextMode};

use crate::error::AppError;

const MAX_ELEMENTS_TO_PARSE: usize = 50_000;

#[derive(Debug)]
pub struct ExtractedArticle {
    pub title: Option<String>,
    pub text: String,
    pub author: Option<String>,
    pub published_at: Option<String>,
    pub modified_at: Option<String>,
}

pub fn extract_html(html: &str, page_url: &str) -> Result<ExtractedArticle, AppError> {
    let config = Config {
        max_elements_to_parse: MAX_ELEMENTS_TO_PARSE,
        text_mode: TextMode::Formatted,
        ..Default::default()
    };
    let mut readability = Readability::new(html, Some(page_url), Some(config))
        .map_err(|_| AppError::extraction("无法解析网页 HTML"))?;
    let article = readability
        .parse()
        .map_err(|_| AppError::extraction("无法从网页中提取正文"))?;

    let text = normalize_text(&article.text_content);
    if text.is_empty() {
        return Err(AppError::extraction("网页中没有可提取的正文"));
    }

    Ok(ExtractedArticle {
        title: non_empty(article.title),
        text,
        author: article.byline.and_then(non_empty),
        published_at: article.published_time.and_then(non_empty),
        modified_at: article.modified_time.and_then(non_empty),
    })
}

pub fn extract_plain_text(text: &str) -> Result<ExtractedArticle, AppError> {
    let text = normalize_text(text);
    if text.is_empty() {
        return Err(AppError::extraction("文本页面内容为空"));
    }
    Ok(ExtractedArticle {
        title: None,
        text,
        author: None,
        published_at: None,
        modified_at: None,
    })
}

fn non_empty(value: impl Into<String>) -> Option<String> {
    let value = value.into().trim().to_owned();
    (!value.is_empty()).then_some(value)
}

fn normalize_text(value: &str) -> String {
    let normalized = value.replace("\r\n", "\n").replace('\r', "\n");
    let mut output = String::with_capacity(normalized.len());
    let mut previous_blank = false;

    for line in normalized.lines() {
        let line = line.trim();
        if line.is_empty() {
            if !previous_blank && !output.is_empty() {
                output.push('\n');
            }
            previous_blank = true;
        } else {
            if !output.is_empty() && !output.ends_with('\n') {
                output.push('\n');
            }
            output.push_str(line);
            previous_blank = false;
        }
    }
    output.trim().to_owned()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn extracts_formatted_article_and_metadata() {
        let html = r#"
            <html><head>
              <title>Fallback title</title>
              <meta name="author" content="Ada">
            </head><body><article>
              <h1>Safe Web Reading</h1>
              <p>This is the first substantial paragraph about secure fetching and evidence.</p>
              <p>This is the second paragraph with enough useful words for extraction.</p>
            </article></body></html>
        "#;

        let article = extract_html(html, "https://example.com/article").unwrap();
        assert!(article.title.is_some());
        assert!(article.text.contains("secure fetching"));
        assert_eq!(article.author.as_deref(), Some("Ada"));
    }

    #[test]
    fn rejects_empty_plain_text() {
        let error = extract_plain_text(" \n \t").unwrap_err();
        assert_eq!(error.error_type(), "EXTRACTION_ERROR");
    }
}
