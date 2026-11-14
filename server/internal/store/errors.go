package store

import (
	"errors"
	"regexp"
	"strings"

	"modernc.org/sqlite"
)

// Error kinds. Every error this package creates is an *Error whose Kind is
// one of these; test with errors.Is. The Python exception each one stands
// for is given in parentheses.
var (
	// ErrStore (StoreError) also matches ErrCorrupt, ErrUnsupported and
	// ErrConflict, which are its subclasses in Python.
	ErrStore = errors.New("store error")
	// ErrCorrupt (StoreCorrupt): the file is physically damaged, is not a
	// configuration store, or its rows do not form a document.
	ErrCorrupt = errors.New("store corrupt")
	// ErrUnsupported (StoreUnsupported): written by a newer storage version.
	ErrUnsupported = errors.New("store unsupported")
	// ErrConflict (StoreConflict): the database changed under a writer that
	// holds the repository lock.
	ErrConflict = errors.New("store conflict")
	// ErrStorage (ConfigStorageError, an OSError): a runtime SQLite failure
	// while reading or committing; keeps the 500 semantics of disk errors.
	// It is not an ErrStore.
	ErrStorage = errors.New("config storage error")
	// ErrSQLite (sqlite3.Error): a SQLite failure that Python lets through
	// untranslated, from Check and BuildTemporary. It is not an ErrStore.
	ErrSQLite = errors.New("sqlite error")
	// ErrDocument (TypeError): the document cannot be stored, either
	// because of its shape or because it holds a value that is not JSON.
	ErrDocument = errors.New("document type error")
)

// Error is an error of the storage engine. Error() returns Message alone,
// which is str(exception) in Python, so messages built from it match.
type Error struct {
	Kind    error  // one of the Err* values
	Message string // the Python exception message
	Cause   error  // the underlying error, if any
}

func (e *Error) Error() string { return e.Message }

func (e *Error) Unwrap() error { return e.Cause }

// Is makes errors.Is(err, ErrX) work, including ErrStore for its subclasses.
func (e *Error) Is(target error) bool {
	if target == e.Kind {
		return true
	}
	return target == ErrStore && (e.Kind == ErrCorrupt || e.Kind == ErrUnsupported || e.Kind == ErrConflict)
}

func newError(kind error, message string) *Error {
	return &Error{Kind: kind, Message: message}
}

// SQLITE_CORRUPT and SQLITE_NOTADB (primary result codes).
const (
	sqliteBusy    = 5
	sqliteLocked  = 6
	sqliteCorrupt = 11
	sqliteNotADB  = 26
)

func sqliteError(err error) (*sqlite.Error, bool) {
	var target *sqlite.Error
	ok := errors.As(err, &target)
	return target, ok
}

func sqliteCode(err error) int {
	if target, ok := sqliteError(err); ok {
		return target.Code() & 0xff
	}
	return 0
}

func isCorrupt(err error) bool {
	code := sqliteCode(err)
	return code == sqliteCorrupt || code == sqliteNotADB
}

var resultCodeSuffix = regexp.MustCompile(` \(\d+\)$`)

// sqliteMessage is str(sqlite3.Error): the text of sqlite3_errmsg. The
// driver formats its errors as "errstr (code)" or "errstr: errmsg (code)",
// with " (SQLITE_BUSY)" appended for that code; errstr never contains ": ".
func sqliteMessage(err error) string {
	target, ok := sqliteError(err)
	if !ok {
		return err.Error()
	}
	message := strings.TrimSuffix(target.Error(), " (SQLITE_BUSY)")
	message = resultCodeSuffix.ReplaceAllString(message, "")
	if _, detail, found := strings.Cut(message, ": "); found {
		return detail
	}
	return message
}

// translate is ConfigStore._translate: SQLite failures become ErrStorage,
// everything else passes through unchanged.
func translate(err error) error {
	if _, ok := sqliteError(err); !ok {
		return err
	}
	if isCorrupt(err) {
		return &Error{Kind: ErrStorage, Message: "配置数据库已损坏：" + sqliteMessage(err), Cause: err}
	}
	return &Error{Kind: ErrStorage, Message: "配置数据库访问失败：" + sqliteMessage(err), Cause: err}
}

// untranslated wraps a SQLite failure that Python raises as it is.
func untranslated(err error) error {
	if _, ok := sqliteError(err); !ok {
		return err
	}
	return &Error{Kind: ErrSQLite, Message: sqliteMessage(err), Cause: err}
}
