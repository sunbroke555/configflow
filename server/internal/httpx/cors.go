package httpx

import (
	"sort"
	"strings"
)

// corsMethods is Flask-CORS's ALL_METHODS joined as it serializes them.
const corsMethods = "GET, HEAD, POST, OPTIONS, PUT, PATCH, DELETE"

// cors is the after-request function of `CORS(app)` (Flask-CORS 4.0 with
// default options: every origin, every header, credentials not supported,
// resources "/*", applied to error responses too).
func cors(c *Ctx, r *Response) {
	h := r.Header()
	if h.Get("Access-Control-Allow-Origin") != "" {
		return
	}
	// resources r'/*' matches every path.
	origin, _ := c.Header("Origin")
	var allowOrigin string
	vary := false
	if origin != "" {
		allowOrigin = origin
		vary = true // ".*" is "probably a regex"
	} else {
		allowOrigin = "*" // always_send with the wildcard origin
	}
	h.Add("Access-Control-Allow-Origin", allowOrigin)
	if c.Request.Method == "OPTIONS" {
		requested, _ := c.Header("Access-Control-Request-Method")
		requested = strings.ToUpper(requested)
		// Flask-CORS tests membership in the serialized string.
		if requested != "" && strings.Contains(corsMethods, requested) {
			if requestHeaders, _ := c.Header("Access-Control-Request-Headers"); requestHeaders != "" {
				var names []string
				for _, name := range strings.Split(requestHeaders, ",") {
					names = append(names, strings.TrimSpace(name))
				}
				sort.Strings(names)
				if joined := strings.Join(names, ", "); joined != "" {
					h.Set("Access-Control-Allow-Headers", joined)
				}
			}
			h.Set("Access-Control-Allow-Methods", corsMethods)
		}
	}
	if vary {
		h.Add("Vary", "Origin")
	}
}
