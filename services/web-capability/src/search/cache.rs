use std::{
    collections::HashMap,
    sync::Arc,
    time::{Duration, Instant},
};

use async_trait::async_trait;
use tokio::sync::Mutex;

use crate::models::SearchResult;

#[derive(Debug, Clone)]
pub struct CachedSearch {
    pub provider: String,
    pub results: Vec<SearchResult>,
}

#[async_trait]
pub trait SearchCache: Send + Sync {
    // async_trait 会重复添加 must_use；这里只豁免两个异步方法的重复提示。
    #[allow(clippy::double_must_use)]
    async fn get(&self, key: &str) -> Option<CachedSearch>;
    #[allow(clippy::double_must_use)]
    async fn put(&self, key: String, value: CachedSearch);
}

struct CacheEntry {
    value: CachedSearch,
    expires_at: Instant,
}

pub struct MemorySearchCache {
    entries: Mutex<HashMap<String, CacheEntry>>,
    ttl: Duration,
    max_entries: usize,
}

impl MemorySearchCache {
    pub fn new(ttl: Duration, max_entries: usize) -> Arc<Self> {
        Arc::new(Self {
            entries: Mutex::new(HashMap::new()),
            ttl,
            max_entries: max_entries.max(1),
        })
    }

    async fn remove_expired(entries: &mut HashMap<String, CacheEntry>) {
        let now = Instant::now();
        entries.retain(|_, entry| entry.expires_at > now);
    }
}

#[async_trait]
impl SearchCache for MemorySearchCache {
    async fn get(&self, key: &str) -> Option<CachedSearch> {
        let mut entries = self.entries.lock().await;
        let expired = entries
            .get(key)
            .is_some_and(|entry| entry.expires_at <= Instant::now());
        if expired {
            entries.remove(key);
            return None;
        }
        entries.get(key).map(|entry| entry.value.clone())
    }

    async fn put(&self, key: String, value: CachedSearch) {
        let mut entries = self.entries.lock().await;
        Self::remove_expired(&mut entries).await;
        if entries.len() >= self.max_entries
            && !entries.contains_key(&key)
            && let Some(oldest_key) = entries.keys().next().cloned()
        {
            entries.remove(&oldest_key);
        }
        entries.insert(
            key,
            CacheEntry {
                value,
                expires_at: Instant::now() + self.ttl,
            },
        );
    }
}
