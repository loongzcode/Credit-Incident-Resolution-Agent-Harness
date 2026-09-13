# ADR-005 Registry Authority Model

## Context
同名工具在不同合作方、产品、环境和协议中可能具有不同权威范围。模型不应自行选择真实端点或凭据。

## Decision
Registry 保存版本化系统/能力/权威规则，路由引用明确的 Case routing revision。可信部署 Adapter Resolver 绑定实际客户端；派发前重新验证 Registry head 和 Case revision。

## Alternatives
把 URL、credential 或 Tool 名字交给模型选择会扩大权限；把所有来源都视为权威会丢失业务适用性。

## Consequences
Registry 不可用时拒绝派发，不能回退任意来源。Admin OIDC 与调查 OIDC audience/permission 分离，发布和回滚保留审计。
