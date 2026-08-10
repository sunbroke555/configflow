package routes

import (
	"fmt"
	"net/http"
	"log"
	"os/exec"
	"strings"
	"io"
)

// RestartService 执行服务重启命令，返回命令输出。
// supervisorctl 场景下补全配置文件路径；restart 失败时回退为 start。
func RestartService(cfg *Config) ([]byte, error) {
	restartCommand := cfg.RestartCommand
	if strings.Contains(restartCommand, "supervisorctl") && !strings.Contains(restartCommand, "-c") {
		restartCommand = strings.Replace(restartCommand, "supervisorctl", "supervisorctl -c /etc/supervisor/supervisord.conf", 1)
		log.Printf("Modified supervisorctl command: %s", restartCommand)
	}

	log.Printf("Executing restart command: %s", restartCommand)
	output, err := executeURLCommand(restartCommand)
	if err == nil {
		return output, nil
	}

	log.Printf("Restart command failed: %v\nOutput: %s", err, string(output))

	// supervisorctl restart 失败时，服务可能未在运行，改用 start
	if strings.Contains(restartCommand, "supervisorctl") && strings.Contains(restartCommand, "restart") {
		startCommand := strings.Replace(restartCommand, "restart", "start", 1)
		log.Printf("Attempting to start service instead: %s", startCommand)
		return executeURLCommand(startCommand)
	}

	return output, err
}

// RestartHandler 处理服务重启请求
func RestartHandler(cfg *Config) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		log.Println("Received restart request.")
		log.Printf("Executing restart command: %s", cfg.RestartCommand)

		// 特殊处理 supervisorctl 命令，确保使用正确的配置文件路径
		restartCommand := cfg.RestartCommand
		if strings.Contains(restartCommand, "supervisorctl") && !strings.Contains(restartCommand, "-c") {
			restartCommand = strings.Replace(restartCommand, "supervisorctl", "supervisorctl -c /etc/supervisor/supervisord.conf", 1)
			log.Printf("Modified supervisorctl command: %s", restartCommand)
		}

		output, err := executeURLCommand(restartCommand)
		if err != nil {
			log.Printf("Restart command failed: %v\nOutput: %s", err, string(output))

			// 如果是 supervisorctl restart 命令失败，尝试用 start 命令
			if strings.Contains(restartCommand, "supervisorctl") && strings.Contains(restartCommand, "restart") {
				log.Printf("Attempting to start service instead of restart...")
				startCommand := strings.Replace(restartCommand, "restart", "start", 1)
				log.Printf("Executing start command: %s", startCommand)

				startOutput, startErr := executeURLCommand(startCommand)
				if startErr != nil {
					log.Printf("Start command also failed: %v\nOutput: %s", startErr, string(startOutput))
					JsonResponse(w, http.StatusInternalServerError, map[string]interface{}{
						"success": false,
						"message": "Restart and start both failed",
						"restart_output": string(output),
						"start_output": string(startOutput),
					})
					return
				}

				log.Println("Service started successfully.")
				JsonResponse(w, http.StatusOK, map[string]string{"success": "true", "message": "Service started (was not running)"})
				return
			}

			JsonResponse(w, http.StatusInternalServerError, map[string]interface{}{"success": false, "message": "Restart failed", "output": string(output)})
			return
		}

		log.Println("Service restarted successfully.")
		JsonResponse(w, http.StatusOK, map[string]string{"success": "true", "message": "Service restarted"})
	}
}

// executeURLCommand 执行URL格式的命令
func executeURLCommand(command string) ([]byte, error) {
	log.Printf("Executing command: %s", command)
	
	// 检查是否为URL格式的命令
	if strings.HasPrefix(command, "http://") || strings.HasPrefix(command, "https://") {
		log.Printf("Executing URL command: %s", command)
		// 发送HTTP请求
		resp, err := http.Post(command, "application/json", nil)
		if err != nil {
			log.Printf("Failed to execute URL command: %v", err)
			return nil, fmt.Errorf("failed to execute URL command: %w", err)
		}
		defer resp.Body.Close()
		
		body, err := io.ReadAll(resp.Body)
		if err != nil {
			log.Printf("Failed to read response: %v", err)
			return nil, fmt.Errorf("failed to read response: %w", err)
		}
		
		if resp.StatusCode >= 400 {
			log.Printf("URL command failed with status %d: %s", resp.StatusCode, string(body))
			return body, fmt.Errorf("URL command failed with status %d: %s", resp.StatusCode, string(body))
		}
		
		log.Printf("URL command executed successfully")
		return body, nil
	}
	
	// 否则执行普通的shell命令
	log.Printf("Executing shell command: %s", command)
	
	// 特殊处理 supervisorctl 命令，确保使用正确的配置文件路径
	if strings.Contains(command, "supervisorctl") && !strings.Contains(command, "-c") {
		// 在命令中添加配置文件路径
		command = strings.Replace(command, "supervisorctl", "supervisorctl -c /etc/supervisor/supervisord.conf", 1)
		log.Printf("Modified supervisorctl command: %s", command)
	}
	
	cmd := exec.Command("sh", "-c", command)
	output, err := cmd.CombinedOutput()
	if err != nil {
		log.Printf("Shell command failed: %v, output: %s", err, string(output))
		return output, err
	}
	log.Printf("Shell command executed successfully")
	return output, nil
}