pub mod brave;
pub mod exa;
pub mod tavily;

use std::sync::Arc;

use async_trait::async_trait;

use crate::{error::AppError, models::SearchResult};

use self::{brave::BraveSearchProvider, exa::ExaSearchProvider, tavily::TavilySearchProvider};

#[derive(Clone)]
pub enum SearchProviderConfig {
    Brave { api_key: Option<String> },
    Tavily { api_key: Option<String> },
    Exa { api_key: Option<String> },
}

impl SearchProviderConfig {
    pub fn name(&self) -> &'static str {
        match self {
            Self::Brave { .. } => "brave",
            Self::Tavily { .. } => "tavily",
            Self::Exa { .. } => "exa",
        }
    }
}

pub fn build_search_provider(
    config: SearchProviderConfig,
) -> Result<Arc<dyn SearchProvider>, AppError> {
    match config {
        SearchProviderConfig::Brave { api_key } => Ok(Arc::new(BraveSearchProvider::new(api_key)?)),
        SearchProviderConfig::Tavily { api_key } => {
            Ok(Arc::new(TavilySearchProvider::new(api_key)?))
        }
        SearchProviderConfig::Exa { api_key } => Ok(Arc::new(ExaSearchProvider::new(api_key)?)),
    }
}

pub fn build_search_providers(
    configs: impl IntoIterator<Item = SearchProviderConfig>,
) -> Result<Vec<Arc<dyn SearchProvider>>, AppError> {
    configs.into_iter().map(build_search_provider).collect()
}

#[async_trait]
pub trait SearchProvider: Send + Sync {
    fn name(&self) -> &'static str;
    fn configured(&self) -> bool;

    async fn search(&self, query: &str, max_results: usize) -> Result<Vec<SearchResult>, AppError>;
}
