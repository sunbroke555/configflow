package auth

import (
	"os"
	"strings"
	"testing"
	"time"

	"configflow/internal/jsonx"
)

type pyjwtCase struct {
	name, token string
	result      *jsonx.Object
}

func loadPyJWT(t *testing.T) (*jsonx.Object, []pyjwtCase) {
	t.Helper()
	data, err := os.ReadFile("testdata/pyjwt_cases.json")
	if err != nil {
		t.Fatal(err)
	}
	value, err := jsonx.Parse(data)
	if err != nil {
		t.Fatal(err)
	}
	doc := value.(*jsonx.Object)
	var cases []pyjwtCase
	for _, item := range doc.GetList("cases") {
		o := item.(*jsonx.Object)
		cases = append(cases, pyjwtCase{o.GetString("name"), o.GetString("token"), o.GetObject("result")})
	}
	return doc, cases
}

// Every token PyJWT 2.8.0 decoded (or refused) decodes the same way here,
// with the same error text.
func TestDecodeMatchesPyJWT(t *testing.T) {
	doc, cases := loadPyJWT(t)
	secret := doc.GetString("secret")
	now := time.Now()
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			claims, err := DecodeHS256(c.token, secret, now)
			if c.result.GetBool("ok") {
				if err != nil {
					t.Fatalf("PyJWT accepted, Go refused: %v", err)
				}
				if !jsonx.Equal(claims, c.result.Value("claims")) {
					t.Fatalf("claims %v, want %v", jsonx.Repr(claims), jsonx.Repr(c.result.Value("claims")))
				}
				return
			}
			if err == nil {
				t.Fatalf("PyJWT refused (%s), Go accepted", c.result.GetString("message"))
			}
			if crash := c.result.GetString("crash"); crash != "" {
				if _, ok := err.(*ClaimTypeError); !ok {
					t.Fatalf("want an uncaught %s, got %v", crash, err)
				}
				return
			}
			te, ok := err.(*TokenError)
			if !ok {
				t.Fatalf("want TokenError, got %T %v", err, err)
			}
			if te.Message != c.result.GetString("message") || te.Expired != c.result.GetBool("expired") {
				t.Fatalf("got %q expired=%v, want %q expired=%v", te.Message, te.Expired,
					c.result.GetString("message"), c.result.GetBool("expired"))
			}
		})
	}
}

// generate_token and jwt.encode produce the same bytes for the same claims.
func TestEncodeMatchesPyJWT(t *testing.T) {
	doc, _ := loadPyJWT(t)
	secret := doc.GetString("secret")
	previous := Configure(Config{SecretKey: secret, Now: func() time.Time {
		return time.Date(2030, 1, 2, 3, 4, 5, 999, time.UTC)
	}})
	defer Configure(previous)
	token, err := GenerateToken("admin")
	if err != nil {
		t.Fatal(err)
	}
	if want := doc.GetObject("encode").GetString("token"); token != want {
		t.Fatalf("token\n%s\nwant\n%s", token, want)
	}
	unicode, _ := EncodeHS256(jsonx.Obj("username", "管理员", "exp", 1900000000, "iat", 1800000000), secret)
	if want := doc.GetObject("encode").GetString("unicode"); unicode != want {
		t.Fatalf("unicode token\n%s\nwant\n%s", unicode, want)
	}
}

func TestParseBearerTokenAcceptsExactPrintableASCIIToken(t *testing.T) {
	for name, c := range map[string][2]string{
		"printable-ascii": {"Bearer abc.DEF-_~+/=", "abc.DEF-_~+/="},
		"maximum-length":  {"Bearer " + strings.Repeat("x", MaxAuthTokenLength), strings.Repeat("x", MaxAuthTokenLength)},
	} {
		if got, ok := ParseBearerToken(c[0], true); !ok || got != c[1] {
			t.Errorf("%s: got %q %v", name, got, ok)
		}
	}
}

func TestParseBearerTokenRejectsMalformedHeaders(t *testing.T) {
	if _, ok := ParseBearerToken("", false); ok {
		t.Error("none")
	}
	for _, header := range []string{"", "Bearer ", "Bearer  token", "Bearer\ttoken", "Bearer token extra",
		" Bearer token", "Bearer token ", "bearer token", "Basic token", "Bearer café", "Bearer 令牌🔐",
		"Bearer token\tmore", "Bearer token\n", "Bearer " + strings.Repeat("x", MaxAuthTokenLength+1)} {
		if got, ok := ParseBearerToken(header, true); ok {
			t.Errorf("%q accepted as %q", header, got)
		}
	}
}

func TestIsTokenWithinLength(t *testing.T) {
	if IsTokenWithinLength("") || !IsTokenWithinLength("令") || IsTokenWithinLength(strings.Repeat("令", MaxAuthTokenLength+1)) ||
		!IsTokenWithinLength(strings.Repeat("令", MaxAuthTokenLength)) {
		t.Error("length bounds")
	}
}

func TestVerifyTokenRoundTripAndExpiry(t *testing.T) {
	clock := time.Date(2026, 11, 14, 12, 0, 0, 0, time.UTC)
	previous := Configure(Config{SecretKey: "s", Now: func() time.Time { return clock }})
	defer Configure(previous)
	token, _ := GenerateToken("admin")
	result, err := VerifyToken(token)
	if err != nil || result.Failed() || result.Claims.GetString("username") != "admin" {
		t.Fatalf("%v %+v", err, result)
	}
	clock = clock.Add(24 * time.Hour)
	if result, _ := VerifyToken(token); result.Error != "expired" {
		t.Fatalf("expired token: %+v", result)
	}
	if result, _ := VerifyToken("abc"); result.Error != "invalid" || result.Detail != "Not enough segments" {
		t.Fatalf("garbage: %+v", result)
	}
}
