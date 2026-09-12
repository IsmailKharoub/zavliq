#!/bin/sh
# Install a specifically selected published Zavliq release. Never edits shell configuration.
set -eu

usage() {
  cat <<'EOF'
Usage: sh install.sh --version v0.1.0 [--install-dir PATH] [--sha256sums-sha256 DIGEST] [--force]

Downloads the matching public GitHub release and verifies SHA256SUMS.
Platforms: macOS 13+ Apple Silicon; Linux x86_64 with glibc 2.35+.
Default destination: $HOME/.local/bin. No shell configuration is changed.
Only use a release version that has actually been published.
Optionally pin the SHA-256 digest of an already reviewed SHA256SUMS manifest.
EOF
}
fail() { printf '%s\n' "zavliq installer: $*" >&2; exit 1; }
version=''
manifest_digest=''
install_dir="${HOME:?HOME is required}/.local/bin"
force=false
while [ "$#" -gt 0 ]; do
  case "$1" in
    --version) [ "$#" -ge 2 ] || fail '--version needs a value'; version=$2; shift 2 ;;
    --sha256sums-sha256) [ "$#" -ge 2 ] || fail '--sha256sums-sha256 needs a digest'; manifest_digest=$2; shift 2 ;;
    --install-dir) [ "$#" -ge 2 ] || fail '--install-dir needs a path'; install_dir=$2; shift 2 ;;
    --force) force=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) fail "Unknown option: $1" ;;
  esac
done
[ -n "$version" ] || { usage >&2; fail 'Select an explicit published version with --version.'; }
printf '%s' "$version" | LC_ALL=C grep -Eq '^v[0-9]+\.[0-9]+\.[0-9]+(-[A-Za-z0-9]+([.-][A-Za-z0-9]+)*)?$' || fail 'Invalid version; expected vMAJOR.MINOR.PATCH with optional prerelease suffix.'
if [ -n "$manifest_digest" ]; then
  printf '%s' "$manifest_digest" | LC_ALL=C grep -Eq '^[0-9a-fA-F]{64}$' || fail 'Expected a 64-character SHA-256 manifest digest.'
  manifest_digest=$(printf '%s' "$manifest_digest" | awk '{print tolower($0)}')
fi
[ -n "$install_dir" ] || fail 'Install directory cannot be empty.'
case "$install_dir" in /*) ;; *) install_dir="$(pwd)/$install_dir" ;; esac
[ ! -L "$install_dir/zavliq" ] || fail 'Destination is a symbolic link; choose another directory or remove it explicitly.'
if [ -e "$install_dir/zavliq" ] && [ "$force" != true ]; then
  fail 'Destination already exists; use --force to replace this executable.'
fi
for dependency in curl tar awk mktemp install; do command -v "$dependency" >/dev/null 2>&1 || fail "Required command missing: $dependency"; done
hash_file() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | awk '{print $1}'
  else
    fail 'Install sha256sum or shasum to verify downloads.'
  fi
}
case "$(uname -s)/$(uname -m)" in
  Darwin/arm64)
    target=aarch64-apple-darwin
    major=$(sw_vers -productVersion | cut -d. -f1)
    [ "$major" -ge 13 ] || fail 'This binary requires macOS 13 or newer.'
    ;;
  Linux/x86_64)
    target=x86_64-unknown-linux-gnu
    libc=$(getconf GNU_LIBC_VERSION 2>/dev/null || true)
    printf '%s\n' "$libc" | awk '{split($2,v,".");exit !($1=="glibc" && (v[1]>2 || (v[1]==2 && v[2]>=35)))}' || fail 'This binary requires glibc 2.35 or newer. Build from source on other Linux systems.'
    ;;
  *) fail 'No published binary for this platform. Build the native runtime from source.' ;;
esac
archive="zavliq-$version-$target.tar.gz"
base="https://github.com/IsmailKharoub/zavliq/releases/download/$version"
work_dir=$(mktemp -d "${TMPDIR:-/tmp}/zavliq-install.XXXXXXXX")
pending_file=''
cleanup() { rm -rf "$work_dir"; if [ -n "$pending_file" ]; then rm -f "$pending_file"; fi; }
trap cleanup EXIT HUP INT TERM
printf '%s\n' "Downloading Zavliq $version for $target..."
curl --fail --show-error --silent --location --proto '=https' --proto-redir '=https' --tlsv1.2 --connect-timeout 10 --max-time 120 --max-filesize 1048576 "$base/SHA256SUMS" -o "$work_dir/SHA256SUMS" || fail 'Checksum manifest unavailable; nothing was installed.'
if [ -n "$manifest_digest" ]; then
  actual_manifest=$(hash_file "$work_dir/SHA256SUMS")
  [ "$actual_manifest" = "$manifest_digest" ] || fail 'Manifest checksum mismatch; the release differs from the reviewed manifest. Nothing was installed.'
fi
expected=$(awk -v archive="$archive" '$2==archive {print $1}' "$work_dir/SHA256SUMS")
printf '%s' "$expected" | LC_ALL=C grep -Eq '^[0-9a-fA-F]{64}$' || fail 'Checksum manifest does not contain exactly one valid matching entry.'
curl --fail --show-error --silent --location --proto '=https' --proto-redir '=https' --tlsv1.2 --connect-timeout 10 --max-time 120 --max-filesize 104857600 "$base/$archive" -o "$work_dir/$archive" || fail 'Release download failed. Confirm this version is public and available.'
actual=$(hash_file "$work_dir/$archive")
[ "$actual" = "$expected" ] || fail 'Checksum mismatch; nothing was installed.'
# Extract only the binary to a known file; never extract archive paths into the destination.
tar -xOf "$work_dir/$archive" zavliq > "$work_dir/zavliq" || fail 'Archive has no zavliq executable.'
[ -s "$work_dir/zavliq" ] || fail 'Archive contains an empty executable.'
mkdir -p "$install_dir"
pending_file=$(mktemp "$install_dir/.zavliq.XXXXXXXX")
install -m 755 "$work_dir/zavliq" "$pending_file"
mv -f "$pending_file" "$install_dir/zavliq"
pending_file=''
printf '%s\n' "Installed: $install_dir/zavliq" 'Run zavliq init YOUR-HANDLE to register, or zavliq pair start @YOUR-ADDRESS to pair an existing identity.'
case ":${PATH:-}:" in
  *":$install_dir:"*) ;;
  *) printf '%s\n' "Add $install_dir to PATH using your shell's usual configuration. No shell files were changed." ;;
esac
