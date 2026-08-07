pub mod fetch;
pub mod health;
pub mod search;

use crate::error::AppError;

pub async fn not_found() -> AppError {
    AppError::not_found()
}

pub async fn method_not_allowed() -> AppError {
    AppError::method_not_allowed()
}
