package preflight

import (
	"context"
	"crypto/rand"
	"encoding/binary"
	"errors"
	"io"
	"net"
	"strconv"
	"strings"
	"time"
)

type wireDNSProbe struct{}

func (wireDNSProbe) Query(parent context.Context, binding portBinding, name string) error {
	question, err := dnsWireQuestion(name)
	if err != nil {
		return err
	}
	var id [2]byte
	if _, err = rand.Read(id[:]); err != nil {
		return err
	}
	request := make([]byte, 12)
	copy(request[:2], id[:])
	request[2] = 1
	request[5] = 1
	request = append(request, question...)
	ctx, cancel := context.WithTimeout(parent, 1200*time.Millisecond)
	defer cancel()
	conn, err := (&net.Dialer{}).DialContext(ctx, binding.Protocol, net.JoinHostPort(binding.Address.String(), strconv.Itoa(binding.Port)))
	if err != nil {
		return errors.New("DNS connection failed")
	}
	defer conn.Close()
	deadline, _ := ctx.Deadline()
	if err = conn.SetDeadline(deadline); err != nil {
		return err
	}
	payload := request
	if binding.Protocol == "tcp" {
		payload = append([]byte{byte(len(request) >> 8), byte(len(request))}, request...)
	}
	if _, err = io.Copy(conn, strings.NewReader(string(payload))); err != nil {
		return errors.New("DNS send failed")
	}
	var response []byte
	if binding.Protocol == "tcp" {
		var length [2]byte
		if _, err = io.ReadFull(conn, length[:]); err != nil {
			return errors.New("DNS read failed")
		}
		size := int(binary.BigEndian.Uint16(length[:]))
		if size < 12 {
			return errors.New("invalid DNS response")
		}
		response = make([]byte, size)
		if _, err = io.ReadFull(conn, response); err != nil {
			return errors.New("DNS read failed")
		}
	} else {
		response = make([]byte, 4096)
		var count int
		count, err = conn.Read(response)
		if err != nil {
			return errors.New("DNS read failed")
		}
		response = response[:count]
	}
	return dnsValidateResponse(response, request, name)
}

func dnsWireQuestion(name string) ([]byte, error) {
	name = strings.TrimSuffix(name, ".")
	if len(name) == 0 || len(name) > 253 {
		return nil, errors.New("invalid query name")
	}
	var result []byte
	for _, label := range strings.Split(name, ".") {
		if len(label) == 0 || len(label) > 63 {
			return nil, errors.New("invalid query name")
		}
		for _, char := range label {
			if char > 127 || !(char >= 'a' && char <= 'z' || char >= 'A' && char <= 'Z' || char >= '0' && char <= '9' || char == '-' || char == '_') {
				return nil, errors.New("invalid query name")
			}
		}
		result = append(result, byte(len(label)))
		result = append(result, label...)
	}
	return append(result, 0, 0, 1, 0, 1), nil
}

func dnsReadName(packet []byte, offset int, depth int) (string, int, error) {
	if depth > 16 {
		return "", 0, errors.New("invalid DNS name")
	}
	var labels []string
	for {
		if offset >= len(packet) {
			return "", 0, errors.New("short DNS name")
		}
		size := int(packet[offset])
		offset++
		if size == 0 {
			return strings.Join(labels, "."), offset, nil
		}
		if size&0xc0 == 0xc0 {
			if offset >= len(packet) {
				return "", 0, errors.New("short DNS pointer")
			}
			target := (size&0x3f)<<8 | int(packet[offset])
			suffix, _, err := dnsReadName(packet, target, depth+1)
			if err != nil {
				return "", 0, err
			}
			labels = append(labels, suffix)
			return strings.Join(labels, "."), offset + 1, nil
		}
		if size > 63 || offset+size > len(packet) {
			return "", 0, errors.New("invalid DNS name")
		}
		labels = append(labels, string(packet[offset:offset+size]))
		offset += size
	}
}

func dnsValidateResponse(packet, request []byte, name string) error {
	invalid := errors.New("DNS response did not contain a valid answer")
	if len(packet) < 12 || len(request) < 12 || packet[0] != request[0] || packet[1] != request[1] || packet[2]&0x80 == 0 || packet[2]&0x7a != 0 || packet[3]&0xf != 0 || binary.BigEndian.Uint16(packet[4:6]) != 1 {
		return invalid
	}
	got, offset, err := dnsReadName(packet, 12, 0)
	if err != nil || !strings.EqualFold(got, strings.TrimSuffix(name, ".")) || offset+4 > len(packet) || binary.BigEndian.Uint16(packet[offset:offset+2]) != 1 || binary.BigEndian.Uint16(packet[offset+2:offset+4]) != 1 {
		return invalid
	}
	offset += 4
	for i := 0; i < int(binary.BigEndian.Uint16(packet[6:8])); i++ {
		_, next, err := dnsReadName(packet, offset, 0)
		if err != nil || next+10 > len(packet) {
			return invalid
		}
		kind := binary.BigEndian.Uint16(packet[next : next+2])
		class := binary.BigEndian.Uint16(packet[next+2 : next+4])
		size := int(binary.BigEndian.Uint16(packet[next+8 : next+10]))
		offset = next + 10 + size
		if offset > len(packet) {
			return invalid
		}
		if kind == 1 && class == 1 && size == 4 {
			return nil
		}
	}
	return invalid
}
