## 变更范围

- [ ] Dev Agent
- [ ] Sandbox Contracts / Client / Gateway
- [ ] Web Capability / Web Tools
- [ ] Contracts / Deploy / Docs

## 验证

- [ ] `uv run pytest`
- [ ] `cargo test --manifest-path services/web-capability/Cargo.toml --all-targets --locked`
- [ ] 未提交 `.env`、Provider Key、Artifact 或构建产物
- [ ] API 契约变化已更新 `contracts/` 并说明兼容性

## 风险与回滚

说明外部调用、费用、安全边界、数据迁移和回滚方式。普通 PR 不应自动触发付费云调用。
