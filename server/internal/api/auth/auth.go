// Package auth holds the routes of backend/routes/auth.py (blueprint "auth",
// prefix /api/auth): status, login, verify. The global gate lives in
// internal/auth.
package auth

import (
	"configflow/internal/auth"
	"configflow/internal/httpx"
	"configflow/internal/jsonx"
	"configflow/internal/logx"
)

var logger = logx.Get("backend.routes.auth")

// Register adds the auth blueprint.
func Register(r *httpx.Router) {
	r.GET("/api/auth/status", "auth.auth_status", authStatus)
	r.POST("/api/auth/login", "auth.login", login)
	r.GET("/api/auth/verify", "auth.verify", auth.Require(verify))
}

func authStatus(c *httpx.Ctx) (*httpx.Response, error) {
	return httpx.JSON(jsonx.Obj("authEnabled", auth.IsAuthEnabled())), nil
}

func login(c *httpx.Ctx) (*httpx.Response, error) {
	fail := func(message string) (*httpx.Response, error) {
		return httpx.Message(500, false, "登录失败。原始错误："+message), nil
	}
	data, err := c.JSON()
	if err != nil {
		return fail(err.Error())
	}
	object, ok := data.(*jsonx.Object)
	if !ok || object == nil {
		return fail(httpx.NoAttributeGet(data))
	}
	username, password := object.Value("username"), object.Value("password")
	if !auth.IsAuthEnabled() {
		return httpx.Message(400, false, "当前未开启登录验证。"), nil
	}
	config := auth.Current()
	if username == config.AdminUsername && password == config.AdminPassword {
		name, _ := username.(string)
		logger.Info("登录成功：用户 " + logx.LogText(username, 120) + "，来源 " + logx.ClientIP(c.Request))
		token, err := auth.GenerateToken(name)
		if err != nil {
			return fail(err.Error())
		}
		return httpx.JSON(jsonx.Obj("success", true, "token", token, "username", name,
			"expiresIn", auth.ExpirationHours*3600)), nil
	}
	logger.Warning("登录失败：用户名或密码错误，用户 " + logx.LogText(username, 120) + "，来源 " + logx.ClientIP(c.Request))
	return httpx.Message(401, false, "用户名或密码错误，请重新输入。"), nil
}

func verify(c *httpx.Ctx) (*httpx.Response, error) {
	return httpx.Message(200, true, "登录状态有效。"), nil
}
