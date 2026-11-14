package auth

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"errors"
	"fmt"
	"math"
	"math/big"
	"strings"
	"time"
	"unicode/utf8"

	"configflow/internal/jsonx"
)

// The JWT code is a port of the parts of PyJWT 2.8.0 the backend uses:
// jwt.encode(payload, key, algorithm="HS256") and
// jwt.decode(token, key, algorithms=["HS256"]) with default options. Error
// texts are PyJWT's, because require_auth shows them to the user.

// TokenError is a PyJWT exception. Expired distinguishes
// ExpiredSignatureError from the other InvalidTokenError subclasses.
type TokenError struct {
	Expired bool
	Message string
}

func (e *TokenError) Error() string { return e.Message }

// ClaimTypeError is the TypeError PyJWT lets escape when a numeric claim is
// not a number or a string (int(None), int([])...): not an InvalidTokenError,
// so Python's verify_token does not catch it and the request fails with 500.
type ClaimTypeError struct{ Message string }

func (e *ClaimTypeError) Error() string { return e.Message }

func decodeError(message string) error { return &TokenError{Message: message} }

// EncodeHS256 is jwt.encode(claims, key, algorithm="HS256"): header
// {"alg":"HS256","typ":"JWT"} (sorted), the claims as compact ASCII JSON in
// their order, unpadded base64url segments, HMAC-SHA256 signature.
func EncodeHS256(claims *jsonx.Object, key string) (string, error) {
	header := `{"alg":"HS256","typ":"JWT"}`
	payload, err := jsonx.Encode(claims, jsonx.Options{EnsureASCII: true, Compact: true})
	if err != nil {
		return "", err
	}
	signingInput := b64(header) + "." + b64(string(payload))
	mac := hmac.New(sha256.New, []byte(key))
	mac.Write([]byte(signingInput))
	return signingInput + "." + base64.RawURLEncoding.EncodeToString(mac.Sum(nil)), nil
}

func b64(s string) string { return base64.RawURLEncoding.EncodeToString([]byte(s)) }

// DecodeHS256 is jwt.decode(token, key, algorithms=["HS256"]) with leeway 0
// at time now. It returns the claims, a *TokenError (Expired for
// ExpiredSignatureError), or a *ClaimTypeError.
func DecodeHS256(token, key string, now time.Time) (*jsonx.Object, error) {
	signingInput, cryptoSegment, ok := cutLast(token, ".")
	if !ok {
		return nil, decodeError("Not enough segments")
	}
	headerSegment, payloadSegment, ok := strings.Cut(signingInput, ".")
	if !ok {
		return nil, decodeError("Not enough segments")
	}
	headerData, err := base64urlDecode(headerSegment)
	if err != nil {
		return nil, decodeError("Invalid header padding")
	}
	header, err := pyJSONLoads(headerData)
	if err != nil {
		return nil, decodeError("Invalid header string: " + err.Error())
	}
	headerObject, ok := header.(*jsonx.Object)
	if !ok || headerObject == nil {
		return nil, decodeError("Invalid header string: must be a json object")
	}
	payloadData, err := base64urlDecode(payloadSegment)
	if err != nil {
		return nil, decodeError("Invalid payload padding")
	}
	signature, err := base64urlDecode(cryptoSegment)
	if err != nil {
		return nil, decodeError("Invalid crypto padding")
	}
	alg, present := headerObject.Get("alg")
	if !present {
		return nil, decodeError("Algorithm not specified")
	}
	if !jsonx.Truthy(alg) || alg != "HS256" {
		return nil, decodeError("The specified alg value is not allowed")
	}
	mac := hmac.New(sha256.New, []byte(key))
	mac.Write([]byte(signingInput))
	if !hmac.Equal(mac.Sum(nil), signature) {
		return nil, decodeError("Signature verification failed")
	}
	payload, err := pyJSONLoads(payloadData)
	if err != nil {
		return nil, decodeError("Invalid payload string: " + err.Error())
	}
	claims, ok := payload.(*jsonx.Object)
	if !ok || claims == nil {
		return nil, decodeError("Invalid payload string: must be a json object")
	}
	if err := validateClaims(claims, now); err != nil {
		return nil, err
	}
	return claims, nil
}

func cutLast(s, sep string) (before, after string, ok bool) {
	i := strings.LastIndex(s, sep)
	if i < 0 {
		return s, "", false
	}
	return s[:i], s[i+len(sep):], true
}

func pyJSONLoads(data []byte) (any, error) {
	if len(data) >= 3 && data[0] == 0xEF && data[1] == 0xBB && data[2] == 0xBF {
		data = data[3:]
	}
	if !utf8.Valid(data) {
		for i := 0; i < len(data); {
			r, size := utf8.DecodeRune(data[i:])
			if r == utf8.RuneError && size <= 1 {
				return nil, fmt.Errorf("'utf-8' codec can't decode byte 0x%02x in position %d: invalid start byte", data[i], i)
			}
			i += size
		}
	}
	return jsonx.Parse(data)
}

// validateClaims is PyJWT 2.8.0 _validate_claims with default options and no
// audience or issuer: iat, nbf, exp in that order, then aud.
func validateClaims(claims *jsonx.Object, now time.Time) error {
	nowSeconds := float64(now.UnixNano()) / 1e9
	if value, ok := claims.Get("iat"); ok {
		iat, err := claimInt(value)
		if err != nil {
			if errors.Is(err, errClaimValue) {
				return decodeError("Issued At claim (iat) must be an integer.")
			}
			return err
		}
		if compareFloat(iat, nowSeconds) > 0 {
			return decodeError("The token is not yet valid (iat)")
		}
	}
	if value, ok := claims.Get("nbf"); ok {
		nbf, err := claimInt(value)
		if err != nil {
			if errors.Is(err, errClaimValue) {
				return decodeError("Not Before claim (nbf) must be an integer.")
			}
			return err
		}
		if compareFloat(nbf, nowSeconds) > 0 {
			return decodeError("The token is not yet valid (nbf)")
		}
	}
	if value, ok := claims.Get("exp"); ok {
		exp, err := claimInt(value)
		if err != nil {
			if errors.Is(err, errClaimValue) {
				return decodeError("Expiration Time claim (exp) must be an integer.")
			}
			return err
		}
		if compareFloat(exp, nowSeconds) <= 0 {
			return &TokenError{Expired: true, Message: "Signature has expired"}
		}
	}
	if value, ok := claims.Get("aud"); ok && jsonx.Truthy(value) {
		return decodeError("Invalid audience")
	}
	return nil
}

var errClaimValue = errors.New("ValueError")

// claimInt is Python's int(value) for a decoded claim: ValueError for text
// that is not an integer and for NaN, TypeError (and OverflowError for
// infinity) for the rest.
func claimInt(value any) (*big.Int, error) {
	switch v := value.(type) {
	case bool:
		if v {
			return big.NewInt(1), nil
		}
		return big.NewInt(0), nil
	case jsonx.Number:
		if v.IsInt() {
			n, ok := new(big.Int).SetString(v.String(), 10)
			if ok {
				return n, nil
			}
		}
		f := v.Float64()
		if math.IsNaN(f) {
			return nil, errClaimValue
		}
		if math.IsInf(f, 0) {
			return nil, &ClaimTypeError{Message: "cannot convert float infinity to integer"}
		}
		n, _ := new(big.Float).SetFloat64(math.Trunc(f)).Int(nil)
		return n, nil
	case string:
		n, ok := pyParseInt(v)
		if !ok {
			return nil, errClaimValue
		}
		return n, nil
	}
	return nil, &ClaimTypeError{Message: "int() argument must be a string, a bytes-like object or a real number, not '" +
		pyTypeName(value) + "'"}
}

func compareFloat(n *big.Int, f float64) int {
	return new(big.Float).SetInt(n).Cmp(big.NewFloat(f))
}

// base64urlDecode is PyJWT's base64url_decode: pad to a multiple of four,
// then base64.urlsafe_b64decode, which ignores characters outside the
// alphabet and stops at the end padding (binascii.a2b_base64, not strict).
func base64urlDecode(segment string) ([]byte, error) {
	if rem := len(segment) % 4; rem > 0 {
		segment += strings.Repeat("=", 4-rem)
	}
	segment = strings.NewReplacer("-", "+", "_", "/").Replace(segment)
	var out []byte
	quadPos, pads := 0, 0
	var leftchar uint32
	for i := 0; i < len(segment); i++ {
		ch := segment[i]
		if ch == '=' {
			pads++
			if quadPos >= 2 && quadPos+pads >= 4 {
				return out, nil
			}
			continue
		}
		value := b64Value(ch)
		if value < 0 {
			continue
		}
		pads = 0
		switch quadPos {
		case 0:
			quadPos, leftchar = 1, uint32(value)
		case 1:
			quadPos = 2
			out = append(out, byte(leftchar<<2|uint32(value)>>4))
			leftchar = uint32(value) & 0x0f
		case 2:
			quadPos = 3
			out = append(out, byte(leftchar<<4|uint32(value)>>2))
			leftchar = uint32(value) & 0x03
		case 3:
			quadPos = 0
			out = append(out, byte(leftchar<<6|uint32(value)))
			leftchar = 0
		}
	}
	if quadPos != 0 {
		if quadPos == 1 {
			return nil, errors.New("Invalid base64-encoded string")
		}
		return nil, errors.New("Incorrect padding")
	}
	return out, nil
}

func b64Value(c byte) int {
	switch {
	case c >= 'A' && c <= 'Z':
		return int(c - 'A')
	case c >= 'a' && c <= 'z':
		return int(c-'a') + 26
	case c >= '0' && c <= '9':
		return int(c-'0') + 52
	case c == '+':
		return 62
	case c == '/':
		return 63
	}
	return -1
}
