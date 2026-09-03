package routes

import (
	"crypto/md5"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"time"
)

// AgentUpdateRequest 更新请求结构
type AgentUpdateRequest struct {
	Version     string `json:"version"`
	DownloadURL string `json:"download_url"`
	MD5Sum      string `json:"md5sum,omitempty"` // 可选的 MD5 校验和
}

// UpdateResponse 更新响应结构
type UpdateResponse struct {
	Success bool   `json:"success"`
	Message string `json:"message"`
}

// HandleUpdate 处理 Agent 更新请求
func HandleUpdate(cfg *Config) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		log.Println("收到更新请求")

		// 解析请求
		var req AgentUpdateRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			log.Printf("解析更新请求失败: %v", err)
			JsonResponse(w, http.StatusBadRequest, UpdateResponse{
				Success: false,
				Message: "Invalid request body",
			})
			return
		}

		log.Printf("更新请求: version=%s", req.Version)

		// 立即返回成功响应（更新在后台进行）
		JsonResponse(w, http.StatusOK, UpdateResponse{
			Success: true,
			Message: "Update started",
		})

		// 在后台执行更新
		go performUpdate(req, cfg)
	}
}

// performUpdate 执行实际的更新操作
func performUpdate(req AgentUpdateRequest, cfg *Config) {
	log.Println("开始后台更新流程...")

	// 等待响应发送完成
	time.Sleep(2 * time.Second)

	// 1. 获取当前二进制文件路径
	currentBinary, err := os.Executable()
	if err != nil {
		log.Printf("获取当前二进制路径失败: %v", err)
		return
	}
	log.Printf("当前二进制路径: %s", currentBinary)

	// 2. 检测系统架构
	arch := detectArchitecture()
	log.Printf("检测到系统架构: %s", arch)

	// 3. 构造下载 URL（使用 Agent 配置中的 ServerURL）
	serverURL := cfg.ServerURL
	if serverURL == "" {
		log.Printf("配置中的 ServerURL 为空，无法下载更新")
		return
	}

	// 去除末尾斜杠
	if serverURL[len(serverURL)-1] == '/' {
		serverURL = serverURL[:len(serverURL)-1]
	}

	downloadURL := fmt.Sprintf("%s/api/agents/download/configflow-agent-%s", serverURL, arch)
	log.Printf("构造下载 URL: %s", redactURLForLog(downloadURL))

	// 4. 创建备份目录
	backupDir := "/opt/configflow-agent/backup"
	if err := os.MkdirAll(backupDir, 0755); err != nil {
		log.Printf("创建备份目录失败: %v", err)
		return
	}

	// 5. 备份当前二进制（固定文件名，每次覆盖）
	backupPath := filepath.Join(backupDir, "configflow-agent.bak")
	log.Printf("备份当前二进制到: %s", backupPath)

	if err := copyFile(currentBinary, backupPath); err != nil {
		log.Printf("备份二进制文件失败: %v", err)
		return
	}

	// 6. 下载新版本
	tempFile := filepath.Join(backupDir, "configflow-agent.tmp")
	log.Printf("下载新版本到: %s", tempFile)

	if err := downloadFile(tempFile, downloadURL); err != nil {
		log.Printf("下载新版本失败: %v", err)
		return
	}

	// 5. 验证下载的文件是否为有效的二进制文件
	if err := verifyBinaryFile(tempFile); err != nil {
		log.Printf("下载的文件验证失败: %v", err)
		os.Remove(tempFile)
		return
	}

	// 6. 验证 MD5（如果提供）
	if req.MD5Sum != "" {
		log.Println("验证文件 MD5...")
		if err := verifyMD5(tempFile, req.MD5Sum); err != nil {
			log.Printf("MD5 校验失败: %v", err)
			os.Remove(tempFile)
			return
		}
		log.Println("MD5 校验通过")
	}

	// 7. 设置可执行权限
	if err := os.Chmod(tempFile, 0755); err != nil {
		log.Printf("设置可执行权限失败: %v", err)
		os.Remove(tempFile)
		return
	}

	// 8. 原子替换二进制文件
	log.Println("替换二进制文件...")
	if err := os.Rename(tempFile, currentBinary); err != nil {
		log.Printf("替换二进制文件失败: %v", err)
		os.Remove(tempFile)
		return
	}

	log.Println("二进制文件替换成功，准备重启...")

	// 9. 重启 Agent 并验证
	if err := restartAgentWithCheck(currentBinary, backupPath); err != nil {
		log.Printf("更新失败: %v", err)
		return
	}

	log.Println("更新流程完成，Agent 已成功重启")
}

// downloadFile 下载文件
func downloadFile(filepath string, url string) error {
	// 创建临时文件
	out, err := os.Create(filepath)
	if err != nil {
		return fmt.Errorf("创建文件失败: %w", err)
	}
	defer out.Close()

	// 下载文件
	client := &http.Client{
		Timeout: 5 * time.Minute, // 5分钟超时
	}

	resp, err := client.Get(url)
	if err != nil {
		return safeURLFailure("从以下地址下载失败:", url, err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("下载失败，状态码: %d", resp.StatusCode)
	}

	// 写入文件
	_, err = io.Copy(out, resp.Body)
	if err != nil {
		return fmt.Errorf("写入文件失败: %w", err)
	}

	return nil
}

// verifyMD5 验证文件 MD5
func verifyMD5(filepath, expectedMD5 string) error {
	file, err := os.Open(filepath)
	if err != nil {
		return fmt.Errorf("打开文件失败: %w", err)
	}
	defer file.Close()

	hash := md5.New()
	if _, err := io.Copy(hash, file); err != nil {
		return fmt.Errorf("计算 MD5 失败: %w", err)
	}

	actualMD5 := fmt.Sprintf("%x", hash.Sum(nil))
	if actualMD5 != expectedMD5 {
		return fmt.Errorf("MD5 不匹配: expected=%s, actual=%s", expectedMD5, actualMD5)
	}

	return nil
}

// verifyBinaryFile 验证文件是否为有效的二进制文件
func verifyBinaryFile(filepath string) error {
	// 读取文件头部，检查 ELF 魔数
	file, err := os.Open(filepath)
	if err != nil {
		return fmt.Errorf("打开文件失败: %w", err)
	}
	defer file.Close()

	// 读取前4个字节
	header := make([]byte, 4)
	if _, err := file.Read(header); err != nil {
		return fmt.Errorf("读取文件头失败: %w", err)
	}

	// 检查 ELF 魔数 (0x7F 'E' 'L' 'F')
	if header[0] != 0x7F || header[1] != 'E' || header[2] != 'L' || header[3] != 'F' {
		return fmt.Errorf("不是有效的 ELF 二进制文件 (可能是 HTML 错误页面)")
	}

	log.Println("文件验证通过，确认为有效的二进制文件")
	return nil
}

// restartAgentWithCheck 重启 Agent 并检查是否成功，失败则回滚
func restartAgentWithCheck(currentBinary, backupPath string) error {
	// 尝试重启
	log.Println("尝试重启 Agent...")
	if err := restartAgent(); err != nil {
		log.Printf("重启失败: %v，开始回滚...", err)
		return rollbackAndRestart(currentBinary, backupPath)
	}

	// 等待一段时间，验证新版本是否正常运行
	log.Println("等待 5 秒验证新版本是否正常运行...")
	time.Sleep(5 * time.Second)

	// 检查进程是否还在运行
	if err := checkAgentRunning(); err != nil {
		log.Printf("新版本启动失败: %v，开始回滚...", err)
		return rollbackAndRestart(currentBinary, backupPath)
	}

	log.Println("新版本启动成功")
	return nil
}

// rollbackAndRestart 回滚到备份版本并重启
func rollbackAndRestart(currentBinary, backupPath string) error {
	log.Println("开始回滚到备份版本...")

	// 检查备份文件是否存在
	if _, err := os.Stat(backupPath); os.IsNotExist(err) {
		return fmt.Errorf("备份文件不存在: %s", backupPath)
	}

	// 恢复备份
	if err := copyFile(backupPath, currentBinary); err != nil {
		return fmt.Errorf("恢复备份失败: %w", err)
	}

	log.Println("备份已恢复，尝试重启...")

	// 重启 Agent
	if err := restartAgent(); err != nil {
		return fmt.Errorf("回滚后重启失败: %w", err)
	}

	log.Println("已成功回滚并重启 Agent")
	return nil
}

// checkAgentRunning 检查 Agent 进程是否在运行
func checkAgentRunning() error {
	// 尝试检测服务状态
	var cmd *exec.Cmd

	if _, err := exec.LookPath("systemctl"); err == nil {
		cmd = exec.Command("systemctl", "is-active", "configflow-agent")
	} else if _, err := exec.LookPath("rc-service"); err == nil {
		cmd = exec.Command("rc-service", "configflow-agent", "status")
	} else if _, err := exec.LookPath("supervisorctl"); err == nil {
		cmd = exec.Command("supervisorctl", "status", "configflow-agent")
	} else {
		// 如果没有服务管理器，假设进程正在运行
		log.Println("无法检测进程状态，假设运行正常")
		return nil
	}

	// 执行检查命令
	if err := cmd.Run(); err != nil {
		return fmt.Errorf("Agent 未运行")
	}

	return nil
}

// restartAgent 重启 Agent
func restartAgent() error {
	// 检测系统类型并执行相应的重启命令
	var cmd *exec.Cmd

	// 尝试检测是否使用 systemd
	if _, err := exec.LookPath("systemctl"); err == nil {
		log.Println("使用 systemctl 重启 Agent...")
		cmd = exec.Command("systemctl", "restart", "configflow-agent")
	} else if _, err := exec.LookPath("rc-service"); err == nil {
		// OpenRC (Alpine Linux)
		log.Println("使用 rc-service 重启 Agent...")
		cmd = exec.Command("rc-service", "configflow-agent", "restart")
	} else if _, err := exec.LookPath("supervisorctl"); err == nil {
		// Supervisor
		log.Println("使用 supervisorctl 重启 Agent...")
		cmd = exec.Command("supervisorctl", "restart", "configflow-agent")
	} else {
		// 如果以上都不可用，直接退出进程让系统重启（假设有自动重启机制）
		log.Println("未找到服务管理器，直接退出进程...")
		time.Sleep(1 * time.Second)
		os.Exit(0)
	}

	// 执行重启命令
	if err := cmd.Run(); err != nil {
		return fmt.Errorf("执行重启命令失败: %w", err)
	}

	return nil
}

// detectArchitecture 检测系统架构
func detectArchitecture() string {
	goos := runtime.GOOS
	goarch := runtime.GOARCH

	// 转换为文件名格式
	switch goarch {
	case "amd64":
		return "linux-amd64"
	case "arm64":
		return "linux-arm64"
	case "arm":
		// 检查是否为 ARMv7
		return "linux-armv7"
	default:
		// 默认返回 amd64
		log.Printf("未知架构: %s/%s，默认使用 linux-amd64", goos, goarch)
		return "linux-amd64"
	}
}
