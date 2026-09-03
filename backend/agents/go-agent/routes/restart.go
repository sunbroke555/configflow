package routes

import (
	"fmt"
	"io"
	"log"
	"net/http"
	"os/exec"
	"strings"
)

// RestartService 执行服务重启命令，返回成功命令的输出。
// supervisorctl 场景下补全配置文件路径；restart 失败时回退为 start。
func RestartService(cfg *Config) ([]byte, error) {
	restartCommand := cfg.RestartCommand
	if strings.Contains(restartCommand, "supervisorctl") && !strings.Contains(restartCommand, "-c") {
		restartCommand = strings.Replace(restartCommand, "supervisorctl", "supervisorctl -c /etc/supervisor/supervisord.conf", 1)
		log.Print("Prepared supervisorctl restart command")
	}

	log.Print("Executing restart command")
	output, err := executeURLCommand(restartCommand)
	if err == nil {
		return output, nil
	}

	log.Print("Restart command failed")

	if strings.Contains(restartCommand, "supervisorctl") && strings.Contains(restartCommand, "restart") {
		startCommand := strings.Replace(restartCommand, "restart", "start", 1)
		log.Print("Attempting to start service instead")
		return executeURLCommand(startCommand)
	}

	return nil, err
}

// RestartHandler 处理服务重启请求
func RestartHandler(cfg *Config) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		log.Println("Received restart request.")
		log.Print("Executing restart command")

		restartCommand := cfg.RestartCommand
		if strings.Contains(restartCommand, "supervisorctl") && !strings.Contains(restartCommand, "-c") {
			restartCommand = strings.Replace(restartCommand, "supervisorctl", "supervisorctl -c /etc/supervisor/supervisord.conf", 1)
			log.Print("Prepared supervisorctl restart command")
		}

		_, err := executeURLCommand(restartCommand)
		if err != nil {
			log.Print("Restart command failed")

			if strings.Contains(restartCommand, "supervisorctl") && strings.Contains(restartCommand, "restart") {
				log.Print("Attempting to start service instead of restart")
				startCommand := strings.Replace(restartCommand, "restart", "start", 1)
				log.Print("Executing start command")

				_, startErr := executeURLCommand(startCommand)
				if startErr != nil {
					log.Print("Start command also failed")
					JsonResponse(w, http.StatusInternalServerError, map[string]interface{}{
						"success": false,
						"message": "Restart and start both failed",
					})
					return
				}

				log.Println("Service started successfully.")
				JsonResponse(w, http.StatusOK, map[string]string{"success": "true", "message": "Service started (was not running)"})
				return
			}

			JsonResponse(w, http.StatusInternalServerError, map[string]interface{}{"success": false, "message": "Restart failed"})
			return
		}

		log.Println("Service restarted successfully.")
		JsonResponse(w, http.StatusOK, map[string]string{"success": "true", "message": "Service restarted"})
	}
}

// executeURLCommand 执行URL或shell格式的命令。
func executeURLCommand(command string) ([]byte, error) {
	log.Print("Executing configured command")

	if strings.HasPrefix(command, "http://") || strings.HasPrefix(command, "https://") {
		log.Print("Executing URL command")
		resp, err := http.Post(command, "application/json", nil)
		if err != nil {
			log.Print("Failed to execute URL command")
			return nil, &safeWrappedError{message: "failed to execute URL command", cause: err}
		}
		defer resp.Body.Close()

		body, err := io.ReadAll(resp.Body)
		if err != nil {
			log.Print("Failed to read URL command response")
			return nil, &safeWrappedError{message: "failed to read URL command response", cause: err}
		}

		if resp.StatusCode >= 400 {
			log.Printf("URL command failed with status %d", resp.StatusCode)
			return nil, fmt.Errorf("URL command failed with status %d", resp.StatusCode)
		}

		log.Print("URL command executed successfully")
		return body, nil
	}

	log.Print("Executing shell command")
	if strings.Contains(command, "supervisorctl") && !strings.Contains(command, "-c") {
		command = strings.Replace(command, "supervisorctl", "supervisorctl -c /etc/supervisor/supervisord.conf", 1)
		log.Print("Prepared supervisorctl shell command")
	}

	cmd := exec.Command("sh", "-c", command)
	output, err := cmd.CombinedOutput()
	if err != nil {
		log.Print("Shell command failed")
		return nil, &safeWrappedError{message: "shell command failed", cause: err}
	}
	log.Print("Shell command executed successfully")
	return output, nil
}
