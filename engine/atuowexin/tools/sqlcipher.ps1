<#
    sqlcipher.ps1 -- SQLCipher v3 / v4 page codec built only on .NET crypto.

    WHY THIS FILE EXISTS
    --------------------
    The Python side of this plugin is not allowed to depend on third party
    packages (no cryptography / pycryptodome), so every cryptographic primitive
    is delegated to .NET through PowerShell:
        System.Security.Cryptography.Aes            (AES-256-CBC, no padding)
        System.Security.Cryptography.Rfc2898DeriveBytes   (PBKDF2)
        System.Security.Cryptography.HMACSHA512 / HMACSHA1
        System.Security.Cryptography.RandomNumberGenerator

    FORMAT REFERENCE (authoritative, taken from the SQLCipher sources)
    -----------------------------------------------------------------
    https://raw.githubusercontent.com/sqlcipher/sqlcipher/v4.5.5/src/crypto_impl.c
      * sqlcipher_codec_ctx_reserve_setup():
            reserve = iv_sz + hmac_sz, rounded UP to a multiple of block_sz (16)
      * sqlcipher_page_hmac(): the MAC input is
            <ciphertext payload> || <16 byte IV> || <page number>
        and the page number is little endian because crypto.h defines
            #define DEFAULT_CIPHER_FLAGS CIPHER_FLAG_HMAC | CIPHER_FLAG_LE_PGNO
      * sqlcipher_page_cipher():
            size  = page_sz - reserve_sz            (ciphertext length)
            iv    = in + size
            hmac  = in + size + iv_sz
        the provider cipher call encrypts exactly `size` bytes -> no padding.
      * sqlcipher_cipher_ctx_key_derive():
            raw hex key   -> used directly as the AES key (no PBKDF2)
            passphrase    -> key = PBKDF2(kdf_algo, pass, salt, kdf_iter, 32)
            hmac key      -> PBKDF2(kdf_algo, key, salt XOR mask, fast_iter, 32)
    https://raw.githubusercontent.com/sqlcipher/sqlcipher/v4.5.5/src/crypto.h
      * FILE_HEADER_SZ 16, PBKDF2_ITER 256000, FAST_PBKDF2_ITER 2,
        HMAC_SALT_MASK 0x3a, CIPHER_MAX_IV_SZ 16
    https://raw.githubusercontent.com/sqlcipher/sqlcipher/v4.5.5/src/crypto.c
      * PRAGMA cipher_compatibility = 3 -> page 1024, HMAC-SHA1,
        PBKDF2-HMAC-SHA1, kdf_iter 64000, use_hmac 1
      * default (v4)                    -> page 4096, HMAC-SHA512,
        PBKDF2-HMAC-SHA512, kdf_iter 256000, use_hmac 1
      * sqlite3Codec(): for pgno == 1 only, the first FILE_HEADER_SZ (16) bytes
        of the page are NOT encrypted; on write they hold the salt, on read they
        are replaced by the "SQLite format 3\0" magic.
    https://www.zetetic.net/sqlcipher/design/ (AES-256-CBC, per page IV stored
    at the end of the page, HMAC of ciphertext + IV, 256000 iterations)

    DERIVED LAYOUT (page size 4096, SQLCipher 4 defaults, reserve = 80)
        page 1 : [16 salt][4000 ciphertext][16 IV][64 HMAC]  = 4096
        page n : [4016 ciphertext][16 IV][64 HMAC]           = 4096
        HMAC input for page 1 is the 4000 ciphertext bytes + 16 IV bytes
        (the salt is NOT part of the MAC input) + pgno (little endian).
    SQLCipher 3 (HMAC-SHA1, reserve = 48 after rounding 36 up to 16):
        page 1 : [16 salt][4032 ciphertext][16 IV][32 reserve, 20 used by HMAC]
        page n : [4048 ciphertext][16 IV][32 reserve, 20 used by HMAC]

    The plaintext (decrypted) page is always the FULL page size; the last
    `reserve` bytes are dead space for SQLite, so they are written as zeros.

    WAL SUPPORT (added in v0.2.0)
    ----------------------------
    WeChat leaves large `-wal` files behind (message_0.db-wal measured at 4 MB,
    1018 frames, 87 commits on a real 4.1.13 install), and every message that
    has not been checkpointed yet lives *only* there.  `-Mode walapply` merges
    such a WAL into the already decrypted database.

    Two facts were established against real WeChat files (17 libraries, 2806
    frames, 100% HMAC-verified) before this mode was written:

      * the 32 byte WAL header and the 24 byte frame headers are PLAINTEXT and
        big endian (SQLite WAL format; magic 0x377f0682 / 0x377f0683);
      * a frame's page payload is encrypted and authenticated EXACTLY like a
        normal database page, with the frame's own `page number` fed into the
        HMAC as a little endian uint32.  The key material is the one derived
        from the *database* salt (page 1 of the main database), not from the
        WAL header salts.

    So `walapply` verifies every frame HMAC and only then overwrites pages of
    the decrypted database, finally truncating it to the page count recorded by
    the last frame that carries a commit (frame header field `db size after
    commit`).  Frames after the last commit are uncommitted and are ignored.

    `walencrypt` is the inverse and exists only so the test suite can build an
    encrypted WAL fixture; like `encrypt` it is not a product feature.

    CLI CONTRACT (the Python module atuowexin/sqlcipher.py calls exactly this)
    ------------------------------------------------------------------------
      -Mode decrypt|encrypt|info|walapply|walencrypt
      -In <file> -Out <file> -Key <hex or passphrase>
      [-KeyType raw|passphrase|auto] [-Version 4|3] [-Salt <hex>] [-Wal]
      [-WalFile <path>] [-PageSize <n>] [-KdfIter <n>]
    The last stdout line is always a single JSON object. Exit code is 0 on
    success and 1 on failure.

    `encrypt` exists so that tests can build encrypted fixtures. It is not a
    product feature.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][ValidateSet('decrypt', 'encrypt', 'info', 'walapply', 'walencrypt')][string]$Mode,
    [string]$In = '',
    [string]$Out = '',
    [string]$Key = '',
    [ValidateSet('raw', 'passphrase', 'auto')][string]$KeyType = 'auto',
    [ValidateSet(3, 4)][int]$Version = 4,
    [string]$Salt = '',
    [switch]$Wal,
    [string]$WalFile = '',
    [int]$PageSize = 0,
    [int]$KdfIter = 0
)

$ErrorActionPreference = 'Stop'

$script:Pages = 0
$script:SaltHex = ''
$script:KeyTypeResolved = $KeyType
$script:PageSizeUsed = 0
$script:ExtraJson = ''
$script:WalStatus = 'n/a'

# b"SQLite format 3\x00" -- 16 bytes, the page 1 prefix that SQLCipher leaves in
# the clear (it holds the salt on disk).
$script:SqliteMagic = [byte[]](0x53, 0x51, 0x4c, 0x69, 0x74, 0x65, 0x20, 0x66,
                               0x6f, 0x72, 0x6d, 0x61, 0x74, 0x20, 0x33, 0x00)

# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

function ConvertTo-JsonText {
    param([string]$Value)
    if ($null -eq $Value) { return '' }
    $sb = New-Object System.Text.StringBuilder
    foreach ($ch in $Value.ToCharArray()) {
        $code = [int][char]$ch
        if ($ch -eq '"') { [void]$sb.Append('\"') }
        elseif ($ch -eq '\') { [void]$sb.Append('\\') }
        elseif ($code -lt 32 -or $code -gt 126) { [void]$sb.Append('\u' + $code.ToString('x4')) }
        else { [void]$sb.Append($ch) }
    }
    return $sb.ToString()
}

function New-ResultJson {
    param(
        [bool]$Ok,
        [string]$ModeName,
        [int]$Pages,
        [int]$VersionNumber,
        [string]$KeyTypeName,
        [string]$SaltValue,
        [string]$ErrorText
    )
    $sb = New-Object System.Text.StringBuilder
    [void]$sb.Append('{')
    [void]$sb.Append('"ok":')
    if ($Ok) { [void]$sb.Append('true') } else { [void]$sb.Append('false') }
    [void]$sb.Append(',"mode":"' + (ConvertTo-JsonText $ModeName) + '"')
    [void]$sb.Append(',"pages":' + $Pages)
    [void]$sb.Append(',"version":' + $VersionNumber)
    [void]$sb.Append(',"keyType":"' + (ConvertTo-JsonText $KeyTypeName) + '"')
    [void]$sb.Append(',"salt":"' + (ConvertTo-JsonText $SaltValue) + '"')
    [void]$sb.Append(',"pageSize":' + $script:PageSizeUsed)
    [void]$sb.Append(',"wal":"' + (ConvertTo-JsonText $script:WalStatus) + '"')
    if ($script:ExtraJson -ne '') { [void]$sb.Append(',' + $script:ExtraJson) }
    [void]$sb.Append(',"error":"' + (ConvertTo-JsonText $ErrorText) + '"')
    [void]$sb.Append('}')
    return $sb.ToString()
}

function Write-ResultAndExit {
    param([bool]$Ok, [string]$ErrorText)
    $json = New-ResultJson -Ok $Ok -ModeName $Mode -Pages $script:Pages `
        -VersionNumber $Version -KeyTypeName $script:KeyTypeResolved `
        -SaltValue $script:SaltHex -ErrorText $ErrorText
    [Console]::Out.WriteLine($json)
    [Console]::Out.Flush()
    if ($Ok) { exit 0 } else { exit 1 }
}

function Get-HexBytes {
    param([string]$Hex)
    if ($null -eq $Hex) { throw 'hex string is null' }
    $Hex = $Hex.Trim()
    if ($Hex.Length % 2 -ne 0) { throw ('hex string must have an even number of characters, got ' + $Hex.Length) }
    if ($Hex.Length -eq 0) { throw 'hex string is empty' }
    $bytes = New-Object byte[] ($Hex.Length / 2)
    for ($i = 0; $i -lt $bytes.Length; $i++) {
        $bytes[$i] = [System.Convert]::ToByte($Hex.Substring($i * 2, 2), 16)
    }
    return , $bytes
}

function ConvertTo-HexString {
    param([byte[]]$Bytes)
    $sb = New-Object System.Text.StringBuilder
    foreach ($b in $Bytes) { [void]$sb.Append($b.ToString('x2')) }
    return $sb.ToString()
}

function Get-Pbkdf2 {
    param([byte[]]$Password, [byte[]]$SaltBytes, [int]$Iterations, [int]$Size, [string]$Algorithm)
    $algo = [System.Security.Cryptography.HashAlgorithmName]::SHA512
    if ($Algorithm -eq 'SHA1') { $algo = [System.Security.Cryptography.HashAlgorithmName]::SHA1 }
    $kdf = [System.Security.Cryptography.Rfc2898DeriveBytes]::new($Password, $SaltBytes, $Iterations, $algo)
    try { $out = $kdf.GetBytes($Size) } finally { $kdf.Dispose() }
    return , $out
}

function Get-KeyMaterial {
    param([string]$KeySpec, [string]$ResolvedType, [byte[]]$SaltBytes, [string]$KdfAlgorithm, [int]$Iterations)
    if ($ResolvedType -eq 'raw') {
        $encKey = Get-HexBytes $KeySpec
        if ($encKey.Length -ne 32) {
            throw ('raw key must be exactly 64 hex characters (32 bytes), got ' + $encKey.Length + ' bytes')
        }
    }
    else {
        $password = [System.Text.Encoding]::UTF8.GetBytes($KeySpec)
        $encKey = Get-Pbkdf2 -Password $password -SaltBytes $SaltBytes -Iterations $Iterations -Size 32 -Algorithm $KdfAlgorithm
    }
    $macSalt = New-Object byte[] $SaltBytes.Length
    for ($i = 0; $i -lt $SaltBytes.Length; $i++) {
        $macSalt[$i] = [byte]([int]$SaltBytes[$i] -bxor 0x3a)
    }
    $macKey = Get-Pbkdf2 -Password $encKey -SaltBytes $macSalt -Iterations 2 -Size 32 -Algorithm $KdfAlgorithm
    return @{ EncKey = $encKey; MacKey = $macKey }
}

function Read-Page {
    param([System.IO.FileStream]$Stream, [byte[]]$Buffer, [int]$Size)
    $read = 0
    while ($read -lt $Size) {
        $n = $Stream.Read($Buffer, $read, $Size - $read)
        if ($n -le 0) { throw ('unexpected end of file, wanted ' + $Size + ' bytes but got ' + $read) }
        $read += $n
    }
}

function Resolve-PageSize {
    param([long]$Length, [int]$Requested)
    if ($Requested -gt 0) {
        if ($Length % $Requested -ne 0) {
            throw ('file length ' + $Length + ' is not a multiple of the requested page size ' + $Requested)
        }
        return $Requested
    }
    foreach ($candidate in @(4096, 1024, 2048, 8192, 512, 16384)) {
        if ($Length % $candidate -eq 0) { return $candidate }
    }
    throw ('cannot determine page size: file length ' + $Length + ' is not a multiple of any known page size')
}

function Test-KeyIsRawHex {
    param([string]$KeySpec)
    if ($KeySpec.Length -ne 64) { return $false }
    foreach ($ch in $KeySpec.ToCharArray()) {
        $isHex = (($ch -ge '0' -and $ch -le '9') -or ($ch -ge 'a' -and $ch -le 'f') -or ($ch -ge 'A' -and $ch -le 'F'))
        if (-not $isHex) { return $false }
    }
    return $true
}

# --------------------------------------------------------------------------
# WAL helpers (SQLite WAL headers are BIG endian; page payloads are SQLCipher
# pages, i.e. little endian HMAC input -- see the file header comment).
# --------------------------------------------------------------------------

$script:WalMagicBE = 0x377f0682
$script:WalMagicLE = 0x377f0683

function Read-BeU32 {
    param([byte[]]$Bytes, [int]$Offset)
    return ([uint32]$Bytes[$Offset] -shl 24) -bor ([uint32]$Bytes[$Offset + 1] -shl 16) -bor `
           ([uint32]$Bytes[$Offset + 2] -shl 8) -bor [uint32]$Bytes[$Offset + 3]
}

function Get-WalChecksum {
    <#
        SQLite's cumulative WAL checksum: s1 += x[i] + s2 ; s2 += x[i+1] + s1
        over 8 byte word pairs, little endian when the WAL magic is 0x377f0682 and
        big endian for 0x377f0683.  SQLCipher encrypts the page *before*
        walEncodeFrame runs, so the checksums cover the ciphertext as stored and
        are a cheap way to stop at a torn write.
    #>
    param([byte[]]$Bytes, [int]$Offset, [int]$Length, [uint32]$S1, [uint32]$S2, [bool]$BigEndian)
    $s1 = [long]$S1
    $s2 = [long]$S2
    for ($i = 0; $i -lt $Length; $i += 8) {
        if ($BigEndian) {
            $w0 = ([long]$Bytes[$Offset + $i] -shl 24) -bor ([long]$Bytes[$Offset + $i + 1] -shl 16) -bor `
                  ([long]$Bytes[$Offset + $i + 2] -shl 8) -bor [long]$Bytes[$Offset + $i + 3]
            $w1 = ([long]$Bytes[$Offset + $i + 4] -shl 24) -bor ([long]$Bytes[$Offset + $i + 5] -shl 16) -bor `
                  ([long]$Bytes[$Offset + $i + 6] -shl 8) -bor [long]$Bytes[$Offset + $i + 7]
        }
        else {
            $w0 = [long]$Bytes[$Offset + $i] -bor ([long]$Bytes[$Offset + $i + 1] -shl 8) -bor `
                  ([long]$Bytes[$Offset + $i + 2] -shl 16) -bor ([long]$Bytes[$Offset + $i + 3] -shl 24)
            $w1 = [long]$Bytes[$Offset + $i + 4] -bor ([long]$Bytes[$Offset + $i + 5] -shl 8) -bor `
                  ([long]$Bytes[$Offset + $i + 6] -shl 16) -bor ([long]$Bytes[$Offset + $i + 7] -shl 24)
        }
        $s1 = ($s1 + $w0 + $s2) % 4294967296
        $s2 = ($s2 + $w1 + $s1) % 4294967296
    }
    return @([uint32]$s1, [uint32]$s2)
}

function Get-PlainDbGeometry {
    <#
        Read page size and reserved-bytes-per-page from a *decrypted* SQLite
        file.  Both are needed to interpret WAL frames, and the decrypted
        header is the authoritative source (it also covers version 3 layouts
        and any non default page size).
    #>
    param([string]$Path)
    $head = New-Object byte[] 100
    $fs = [System.IO.File]::Open($Path, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite)
    try { Read-Page -Stream $fs -Buffer $head -Size 100 } finally { $fs.Dispose() }
    for ($i = 0; $i -lt 16; $i++) {
        if ($head[$i] -ne $script:SqliteMagic[$i]) {
            throw ('not a plaintext SQLite database: ' + $Path)
        }
    }
    $ps = ([int]$head[16] -shl 8) -bor [int]$head[17]
    if ($ps -eq 1) { $ps = 65536 }
    if ($ps -lt 512 -or $ps -gt 65536 -or ($ps -band ($ps - 1)) -ne 0) {
        throw ('implausible SQLite page size ' + $ps + ' in ' + $Path)
    }
    $reserve = [int]$head[20]
    if ($reserve -lt 0 -or $reserve -ge $ps) {
        throw ('implausible reserved byte count ' + $reserve + ' in ' + $Path)
    }
    # header byte 19 is SQLite's "read format": 1 = rollback journal, 2 = WAL.
    return @{ PageSize = $ps; Reserve = $reserve; ReadVersion = [int]$head[19] }
}

function New-WalCodec {
    param([int]$VersionNumber, [byte[]]$EncKey, [byte[]]$MacKey)
    $hmacSize = 64
    $hmac = [System.Security.Cryptography.HMACSHA512]::new($MacKey)
    if ($VersionNumber -eq 3) {
        $hmacSize = 20
        $hmac = [System.Security.Cryptography.HMACSHA1]::new($MacKey)
    }
    $aes = [System.Security.Cryptography.Aes]::Create()
    $aes.Mode = [System.Security.Cryptography.CipherMode]::CBC
    $aes.Padding = [System.Security.Cryptography.PaddingMode]::None
    $aes.KeySize = 256
    $aes.Key = $EncKey
    return @{ Aes = $aes; Hmac = $hmac; HmacSize = $hmacSize }
}

function Invoke-WalApply {
    <#
        Merge an encrypted SQLCipher WAL into an already decrypted database.

        Every frame of the committed prefix is HMAC verified before a single
        byte is written; a mismatch aborts (the caller deletes the partial
        output).  Returns frame/commit/page counts for the JSON result.
    #>
    param(
        [string]$PlainDb,
        [string]$WalPath,
        [string]$OutPath,
        [byte[]]$EncKey,
        [byte[]]$MacKey,
        [int]$VersionNumber
    )
    $geo = Get-PlainDbGeometry -Path $PlainDb
    $pageSize = [int]$geo.PageSize
    $reserve = [int]$geo.Reserve
    if ($reserve -lt 16) { throw ('reserved byte count ' + $reserve + ' is too small for an IV') }
    if ([int]$geo.ReadVersion -ne 2) {
        # The database header says it uses a rollback journal, so a sibling -wal
        # does not belong to it.  sqlite3's pagerOpenWalIfPresent() applies the
        # same rule; merging here would mix two different files.
        return @{ Pages = 0; Frames = 0; ValidFrames = 0; Committed = 0; Applied = 0; `
                  LastCommit = 0; Stale = $true; HeaderOk = $true; NotWalMode = $true; Normalized = $false }
    }

    $walLength = (Get-Item -LiteralPath $WalPath).Length
    if ($walLength -lt 32) { throw ('wal file is too short to hold a header (' + $walLength + ' bytes)') }
    $walBytes = [System.IO.File]::ReadAllBytes($WalPath)

    $magic = Read-BeU32 -Bytes $walBytes -Offset 0
    if ($magic -ne $script:WalMagicBE -and $magic -ne $script:WalMagicLE) {
        throw ('not a SQLite WAL file (magic 0x' + $magic.ToString('x8') + '): ' + $WalPath)
    }
    $walPageSize = [int](Read-BeU32 -Bytes $walBytes -Offset 8)
    if ($walPageSize -ne $pageSize) {
        throw ('wal page size ' + $walPageSize + ' does not match database page size ' + $pageSize)
    }

    $frameSize = 24 + $pageSize
    $frameCount = [int](($walLength - 32) / $frameSize)
    if ($frameCount -lt 1) { throw 'wal file holds no complete frame' }

    # A frame is only valid when its salt-1/salt-2 equal the WAL header's, and
    # reading stops at the first frame that does not match (exactly what
    # sqlite3 does in walIndexRecover).  WeChat leaves *reset* WALs behind: the
    # header is rewritten with salt-1 + 1 and a fresh salt-2 while the old
    # frames stay in the file.  Those frames are already checkpointed into the
    # database, so treating them as new data corrupts it (measured: applying
    # message_0.db-wal took 3297 rows down to 2961 and session.db became a
    # "database disk image is malformed").
    $walSalt1 = Read-BeU32 -Bytes $walBytes -Offset 16
    $walSalt2 = Read-BeU32 -Bytes $walBytes -Offset 20
    $walBigEndian = ($magic -eq $script:WalMagicLE)

    # The header checksum must validate, otherwise SQLite treats the whole WAL as
    # empty (a torn header write).
    $headCk = Get-WalChecksum -Bytes $walBytes -Offset 0 -Length 24 -S1 ([uint32]0) -S2 ([uint32]0) -BigEndian $walBigEndian
    $headOk = ($headCk[0] -eq (Read-BeU32 -Bytes $walBytes -Offset 24)) -and `
              ($headCk[1] -eq (Read-BeU32 -Bytes $walBytes -Offset 28))
    if (-not $headOk) {
        return @{ Pages = 0; Frames = $frameCount; ValidFrames = 0; `
                  Committed = 0; Applied = 0; LastCommit = 0; Stale = $true; HeaderOk = $false; Corrupt = $true }
    }

    $validFrames = 0
    $lastCommit = 0
    $commitCount = 0
    $committedPages = 0
    $corrupt = $false
    $chain = @([uint32]$headCk[0], [uint32]$headCk[1])
    for ($i = 0; $i -lt $frameCount; $i++) {
        $off = 32 + ($i * $frameSize)
        $fs1 = Read-BeU32 -Bytes $walBytes -Offset ($off + 8)
        $fs2 = Read-BeU32 -Bytes $walBytes -Offset ($off + 12)
        # salt mismatch = stale generation (a reset WAL) -> ignore the rest
        if ($fs1 -ne $walSalt1 -or $fs2 -ne $walSalt2) { break }
        # checksum chain: frame header (first 8 bytes) then the stored page image
        $chain = Get-WalChecksum -Bytes $walBytes -Offset $off -Length 8 -S1 $chain[0] -S2 $chain[1] -BigEndian $walBigEndian
        $chain = Get-WalChecksum -Bytes $walBytes -Offset ($off + 24) -Length $pageSize -S1 $chain[0] -S2 $chain[1] -BigEndian $walBigEndian
        if ($chain[0] -ne (Read-BeU32 -Bytes $walBytes -Offset ($off + 16)) -or `
            $chain[1] -ne (Read-BeU32 -Bytes $walBytes -Offset ($off + 20))) {
            # salts matched but the chain broke: a torn/truncated WAL, not a stale one
            $corrupt = $true
            break
        }
        $validFrames++
        $dbSizeAfter = [int](Read-BeU32 -Bytes $walBytes -Offset ($off + 4))
        if ($dbSizeAfter -ne 0) {
            $lastCommit = $i + 1
            $committedPages = $dbSizeAfter
            $commitCount++
        }
    }
    if ($lastCommit -eq 0) {
        return @{ Pages = $committedPages; Frames = $frameCount; ValidFrames = $validFrames; `
                  Committed = 0; Applied = 0; LastCommit = 0; Stale = ($validFrames -eq 0); `
                  HeaderOk = $true; Corrupt = $corrupt; Normalized = $false }
    }

    $codec = New-WalCodec -VersionNumber $VersionNumber -EncKey $EncKey -MacKey $MacKey
    $aes = $codec.Aes
    $hmac = $codec.Hmac
    $hmacSize = [int]$codec.HmacSize

    [System.IO.File]::Copy($PlainDb, $OutPath, $true)
    $out = [System.IO.File]::Open($OutPath, [System.IO.FileMode]::Open, [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)
    try {
        $macBuf = New-Object byte[] ($pageSize + 4)
        $zeros = New-Object byte[] $reserve
        $pgnoBytes = New-Object byte[] 4
        $iv = New-Object byte[] 16
        for ($i = 0; $i -lt $lastCommit; $i++) {
            $off = 32 + ($i * $frameSize)
            $pgno = [int](Read-BeU32 -Bytes $walBytes -Offset $off)
            if ($pgno -lt 1) { throw ('frame ' + ($i + 1) + ' has an invalid page number ' + $pgno) }
            $skip = 0
            if ($pgno -eq 1) { $skip = 16 }
            $ctLen = $pageSize - $reserve - $skip
            if ($ctLen -le 0 -or ($ctLen % 16) -ne 0) {
                throw ('frame ' + ($i + 1) + ' has an impossible ciphertext length ' + $ctLen)
            }
            $pageOff = $off + 24
            $ivOffset = $pageOff + $skip + $ctLen
            $macOffset = $ivOffset + 16

            $pgnoBytes[0] = [byte]($pgno -band 0xff)
            $pgnoBytes[1] = [byte](($pgno -shr 8) -band 0xff)
            $pgnoBytes[2] = [byte](($pgno -shr 16) -band 0xff)
            $pgnoBytes[3] = [byte](($pgno -shr 24) -band 0xff)

            [System.Array]::Copy($walBytes, $pageOff + $skip, $macBuf, 0, $ctLen)
            [System.Array]::Copy($walBytes, $ivOffset, $macBuf, $ctLen, 16)
            [System.Array]::Copy($pgnoBytes, 0, $macBuf, $ctLen + 16, 4)
            $mac = $hmac.ComputeHash($macBuf, 0, $ctLen + 20)
            $diff = 0
            for ($b = 0; $b -lt $hmacSize; $b++) { $diff = $diff -bor ($mac[$b] -bxor $walBytes[$macOffset + $b]) }
            if ($diff -ne 0) {
                throw ('hmac mismatch on wal frame ' + ($i + 1) + ' (page ' + $pgno + '); the wal does not belong to this key/database')
            }
            [System.Array]::Copy($walBytes, $ivOffset, $iv, 0, 16)
            $dec = $aes.CreateDecryptor($EncKey, $iv)
            try { $plain = $dec.TransformFinalBlock($walBytes, $pageOff + $skip, $ctLen) } finally { $dec.Dispose() }

            $out.Seek([long]($pgno - 1) * $pageSize, [System.IO.SeekOrigin]::Begin) | Out-Null
            if ($pgno -eq 1) { $out.Write($script:SqliteMagic, 0, 16) }
            $out.Write($plain, 0, $plain.Length)
            $out.Write($zeros, 0, $reserve)
        }
        $out.SetLength([long]$committedPages * $pageSize)
        # Normalise page 1: SQLite trusts the in-header page count, and reports
        # SQLITE_CORRUPT when it exceeds the file size, so make the header agree
        # with the file we just produced (and keep version-valid-for ==
        # change-counter, which SQLite writes that way itself).
        $normalized = $false
        if ($committedPages -gt 0) {
            $head = New-Object byte[] 100
            $out.Seek(0, [System.IO.SeekOrigin]::Begin) | Out-Null
            $read = 0
            while ($read -lt 100) {
                $n = $out.Read($head, $read, 100 - $read)
                if ($n -le 0) { break }
                $read += $n
            }
            $headerPages = [int](Read-BeU32 -Bytes $head -Offset 28)
            if ($headerPages -ne $committedPages) {
                $head[28] = [byte](($committedPages -shr 24) -band 0xff)
                $head[29] = [byte](($committedPages -shr 16) -band 0xff)
                $head[30] = [byte](($committedPages -shr 8) -band 0xff)
                $head[31] = [byte]($committedPages -band 0xff)
                $head[92] = $head[24]
                $head[93] = $head[25]
                $head[94] = $head[26]
                $head[95] = $head[27]
                $out.Seek(0, [System.IO.SeekOrigin]::Begin) | Out-Null
                $out.Write($head, 0, 100)
                $normalized = $true
            }
        }
        $out.Flush()
    }
    finally { $out.Dispose() }

    return @{ Pages = $committedPages; Frames = $frameCount; ValidFrames = $validFrames; `
              Committed = $commitCount; Applied = $lastCommit; LastCommit = $lastCommit; `
              Stale = $false; HeaderOk = $true; Corrupt = $false; Normalized = $normalized }
}

function Set-BeU32 {
    param([byte[]]$Bytes, [int]$Offset, [uint32]$Value)
    $Bytes[$Offset] = [byte](($Value -shr 24) -band 0xff)
    $Bytes[$Offset + 1] = [byte](($Value -shr 16) -band 0xff)
    $Bytes[$Offset + 2] = [byte](($Value -shr 8) -band 0xff)
    $Bytes[$Offset + 3] = [byte]($Value -band 0xff)
}

function Invoke-WalEncrypt {
    <#
        Test-only inverse of Invoke-WalApply: encrypt the page payload of every
        frame of a *plaintext* SQLite WAL.

        It reproduces the on-disk shape measured on real WeChat data faithfully:
        the 32 byte WAL header is copied through unchanged (it holds no encrypted
        bytes, and SQLite wrote its own checksum), the frame salt fields are
        copied, and the cumulative frame checksum is recomputed over the
        **ciphertext as stored** - SQLCipher encrypts the page before SQLite's
        walEncodeFrame() runs, so that is where the checksum comes from.
    #>
    param(
        [string]$PlainWal,
        [string]$OutPath,
        [byte[]]$EncKey,
        [byte[]]$MacKey,
        [int]$VersionNumber,
        [int]$PageSizeOverride
    )
    $walBytes = [System.IO.File]::ReadAllBytes($PlainWal)
    if ($walBytes.Length -lt 32) { throw 'source wal is too short to hold a header' }
    $magic = Read-BeU32 -Bytes $walBytes -Offset 0
    if ($magic -ne $script:WalMagicBE -and $magic -ne $script:WalMagicLE) {
        throw ('source is not a SQLite WAL file (magic 0x' + $magic.ToString('x8') + ')')
    }
    $bigEndian = ($magic -eq $script:WalMagicLE)
    $pageSize = [int](Read-BeU32 -Bytes $walBytes -Offset 8)
    if ($PageSizeOverride -gt 0) { $pageSize = $PageSizeOverride }
    $reserveRaw = 16 + 64
    if ($VersionNumber -eq 3) { $reserveRaw = 16 + 20 }
    $reserve = $reserveRaw
    $rem = $reserveRaw % 16
    if ($rem -ne 0) { $reserve = $reserveRaw + (16 - $rem) }

    $frameSize = 24 + $pageSize
    $frameCount = [int](($walBytes.Length - 32) / $frameSize)

    $codec = New-WalCodec -VersionNumber $VersionNumber -EncKey $EncKey -MacKey $MacKey
    $aes = $codec.Aes
    $hmac = $codec.Hmac
    $hmacSize = [int]$codec.HmacSize

    $macBuf = New-Object byte[] ($pageSize + 4)
    $pgnoBytes = New-Object byte[] 4
    $iv = New-Object byte[] 16
    $image = New-Object byte[] $pageSize
    $frameHdr = New-Object byte[] 24
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    $chain = @([uint32](Read-BeU32 -Bytes $walBytes -Offset 24), [uint32](Read-BeU32 -Bytes $walBytes -Offset 28))
    $out = [System.IO.File]::Create($OutPath)
    try {
        $out.Write($walBytes, 0, 32)
        for ($i = 0; $i -lt $frameCount; $i++) {
            $off = 32 + ($i * $frameSize)
            $pgno = [int](Read-BeU32 -Bytes $walBytes -Offset $off)
            $skip = 0
            if ($pgno -eq 1) { $skip = 16 }
            $ctLen = $pageSize - $reserve - $skip
            if ($ctLen -le 0 -or ($ctLen % 16) -ne 0) {
                throw ('frame ' + ($i + 1) + ' has an impossible ciphertext length ' + $ctLen)
            }
            [System.Array]::Clear($image, 0, $pageSize)
            if ($skip -gt 0) { [System.Array]::Copy($walBytes, $off + 24, $image, 0, 16) }
            $rng.GetBytes($iv)
            $enc = $aes.CreateEncryptor($EncKey, $iv)
            try { $ct = $enc.TransformFinalBlock($walBytes, $off + 24 + $skip, $ctLen) } finally { $enc.Dispose() }
            [System.Array]::Copy($ct, 0, $image, $skip, $ctLen)
            [System.Array]::Copy($iv, 0, $image, $skip + $ctLen, 16)

            $pgnoBytes[0] = [byte]($pgno -band 0xff)
            $pgnoBytes[1] = [byte](($pgno -shr 8) -band 0xff)
            $pgnoBytes[2] = [byte](($pgno -shr 16) -band 0xff)
            $pgnoBytes[3] = [byte](($pgno -shr 24) -band 0xff)
            [System.Array]::Copy($ct, 0, $macBuf, 0, $ctLen)
            [System.Array]::Copy($iv, 0, $macBuf, $ctLen, 16)
            [System.Array]::Copy($pgnoBytes, 0, $macBuf, $ctLen + 16, 4)
            $mac = $hmac.ComputeHash($macBuf, 0, $ctLen + 20)
            [System.Array]::Copy($mac, 0, $image, $skip + $ctLen + 16, $hmacSize)

            # frame header: page number / db size / salts copied, checksums recomputed
            [System.Array]::Copy($walBytes, $off, $frameHdr, 0, 16)
            $chain = Get-WalChecksum -Bytes $frameHdr -Offset 0 -Length 8 -S1 $chain[0] -S2 $chain[1] -BigEndian $bigEndian
            $chain = Get-WalChecksum -Bytes $image -Offset 0 -Length $pageSize -S1 $chain[0] -S2 $chain[1] -BigEndian $bigEndian
            Set-BeU32 -Bytes $frameHdr -Offset 16 -Value $chain[0]
            Set-BeU32 -Bytes $frameHdr -Offset 20 -Value $chain[1]
            $out.Write($frameHdr, 0, 24)
            $out.Write($image, 0, $pageSize)
        }
        # a torn trailing frame in the source is copied verbatim (never a real frame)
        $tail = $walBytes.Length - 32 - ($frameCount * $frameSize)
        if ($tail -gt 0) { $out.Write($walBytes, 32 + ($frameCount * $frameSize), $tail) }
        $out.Flush()
    }
    finally {
        $out.Dispose()
        $rng.Dispose()
        $aes.Dispose()
        $hmac.Dispose()
    }
    return @{ Frames = $frameCount; PageSize = $pageSize }
}

# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

$errMsg = ''
try {
    $InPath = $In
    $OutPath = $Out

    if ($Mode -eq 'info') {
        if ($InPath -eq '' -or -not (Test-Path -LiteralPath $InPath)) {
            throw ('info mode requires -In pointing at an existing file')
        }
        $item = Get-Item -LiteralPath $InPath
        $script:PageSizeUsed = Resolve-PageSize -Length $item.Length -Requested $PageSize
        $script:Pages = [int]($item.Length / $script:PageSizeUsed)
        $head = New-Object byte[] 16
        $fs = [System.IO.File]::Open($InPath, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite)
        try { Read-Page -Stream $fs -Buffer $head -Size 16 } finally { $fs.Dispose() }
        $script:SaltHex = ConvertTo-HexString $head
        $isPlain = $true
        for ($i = 0; $i -lt 16; $i++) { if ($head[$i] -ne $script:SqliteMagic[$i]) { $isPlain = $false } }
        # capability probe: PBKDF2-HMAC-SHA512 must exist in this host
        $probe = Get-Pbkdf2 -Password ([byte[]](1, 2, 3, 4)) -SaltBytes ([byte[]](5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16)) -Iterations 1 -Size 32 -Algorithm 'SHA512'
        if ($probe.Length -ne 32) { throw 'KDF capability probe failed' }
        $script:WalStatus = 'reported'
        $script:ExtraJson = '"size":' + $item.Length + ',"plaintextSqlite":' + $(if ($isPlain) { 'true' } else { 'false' }) + ',"kdfSha512":true'
        Write-ResultAndExit -Ok $true -ErrorText ''
    }

    if ($InPath -eq '' -or -not (Test-Path -LiteralPath $InPath)) { throw 'source file not found: ' + $InPath }
    if ($OutPath -eq '') { throw 'missing -Out' }
    if ($Key -eq '') { throw 'missing -Key' }

    if ($Mode -eq 'walapply') {
        if ($WalFile -eq '') { throw 'walapply requires -WalFile pointing at the encrypted -wal file' }
        if (-not (Test-Path -LiteralPath $WalFile -PathType Leaf)) { throw ('wal file not found: ' + $WalFile) }
        if ($Salt -eq '') { throw 'walapply requires -Salt (the 16 byte salt of the encrypted database, as hex)' }
        if ([System.IO.Path]::GetFullPath($OutPath) -eq [System.IO.Path]::GetFullPath($InPath)) {
            throw 'walapply refuses to overwrite -In in place: pass a different -Out'
        }
    }
    if ($Mode -eq 'walencrypt' -and $Salt -eq '') {
        throw 'walencrypt requires -Salt (the 16 byte salt of the encrypted database, as hex)'
    }

    if ($KeyType -eq 'auto') {
        if (Test-KeyIsRawHex $Key) { $script:KeyTypeResolved = 'raw' } else { $script:KeyTypeResolved = 'passphrase' }
    }
    else {
        $script:KeyTypeResolved = $KeyType
    }

    $kdfAlgorithm = 'SHA512'
    $hmacType = 'HMACSHA512'
    $hmacSize = 64
    $iterations = 256000
    if ($Version -eq 3) {
        $kdfAlgorithm = 'SHA1'
        $hmacType = 'HMACSHA1'
        $hmacSize = 20
        $iterations = 64000
    }
    if ($KdfIter -gt 0) { $iterations = $KdfIter }

    $ivSize = 16
    $reserveRaw = $ivSize + $hmacSize
    # SQLCipher rounds the reserve up to a whole number of cipher blocks.
    # NB: do NOT use [int](($x + 15) / 16): a PowerShell [int] cast rounds to
    # nearest instead of truncating, which yields 96 instead of 80 for v4.
    $reserve = $reserveRaw
    $reserveRem = $reserveRaw % 16
    if ($reserveRem -ne 0) { $reserve = $reserveRaw + (16 - $reserveRem) }

    $fsIn = [System.IO.File]::Open($InPath, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite)
    try {
        $length = $fsIn.Length
        if ($Mode -eq 'walencrypt') {
            # -In is a WAL file, whose length is 32 + n * (24 + page size): the
            # page size has to come from the WAL header, not from the length.
            if ($length -lt 32) { throw 'source wal is too short to hold a header' }
            $walHead = New-Object byte[] 32
            Read-Page -Stream $fsIn -Buffer $walHead -Size 32
            $fsIn.Position = 0
            $pageSize = [int](Read-BeU32 -Bytes $walHead -Offset 8)
            if ($PageSize -gt 0) { $pageSize = $PageSize }
            if ($pageSize -lt 512 -or $pageSize -gt 65536 -or ($pageSize -band ($pageSize - 1)) -ne 0) {
                throw ('implausible wal page size ' + $pageSize)
            }
            $script:PageSizeUsed = $pageSize
            $script:Pages = [int](($length - 32) / (24 + $pageSize))
        }
        else {
            $script:PageSizeUsed = Resolve-PageSize -Length $length -Requested $PageSize
            $pageSize = $script:PageSizeUsed
            $script:Pages = [int]($length / $pageSize)
        }
        if ($script:Pages -lt 1) { throw 'input file is empty' }
        $payloadFull = $pageSize - $reserve          # ciphertext size for page >= 2
        $payloadFirst = $payloadFull - 16            # ciphertext size for page 1

        # ---- salt -------------------------------------------------------
        if ($Salt -ne '') {
            $saltBytes = Get-HexBytes $Salt
            if ($saltBytes.Length -ne 16) { throw ('-Salt must be 32 hex characters (16 bytes), got ' + $saltBytes.Length + ' bytes') }
        }
        elseif ($Mode -eq 'decrypt' -or $Mode -eq 'info') {
            $saltBytes = New-Object byte[] 16
            Read-Page -Stream $fsIn -Buffer $saltBytes -Size 16
            $fsIn.Position = 0
        }
        else {
            $saltBytes = New-Object byte[] 16
            $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
            try { $rng.GetBytes($saltBytes) } finally { $rng.Dispose() }
        }
        $script:SaltHex = ConvertTo-HexString $saltBytes

        $material = Get-KeyMaterial -KeySpec $Key -ResolvedType $script:KeyTypeResolved `
            -SaltBytes $saltBytes -KdfAlgorithm $kdfAlgorithm -Iterations $iterations
        $encKey = $material.EncKey
        $macKey = $material.MacKey

        if ($Mode -eq 'walapply') {
            $walStats = Invoke-WalApply -PlainDb $InPath -WalPath $WalFile -OutPath $OutPath `
                -EncKey $encKey -MacKey $macKey -VersionNumber $Version
            $script:Pages = [int]$walStats.Pages
            if ($walStats.Corrupt) { $script:WalStatus = 'invalid' }
            elseif ($walStats.Stale) { $script:WalStatus = 'stale-frames-ignored' }
            elseif ($walStats.Applied -gt 0) { $script:WalStatus = 'applied' }
            else { $script:WalStatus = 'no-commit-frame' }
            $bool = { param($v) if ($v) { return 'true' } return 'false' }
            $staleText = (& $bool $walStats.Stale)
            $corruptText = (& $bool $walStats.Corrupt)
            $normalizedText = (& $bool $walStats.Normalized)
            $script:ExtraJson = '"walFrames":' + $walStats.Frames + ',"walValidFrames":' + $walStats.ValidFrames + `
                ',"walCommitted":' + $walStats.Committed + ',"walApplied":' + $walStats.Applied + `
                ',"walPageCount":' + $walStats.Pages + ',"walStale":' + $staleText + `
                ',"walCorrupt":' + $corruptText + ',"walNormalized":' + $normalizedText
            Write-ResultAndExit -Ok $true -ErrorText ''
        }
        elseif ($Mode -eq 'walencrypt') {
            $walStats = Invoke-WalEncrypt -PlainWal $InPath -OutPath $OutPath `
                -EncKey $encKey -MacKey $macKey -VersionNumber $Version -PageSizeOverride $PageSize
            $script:Pages = [int]$walStats.Frames
            $script:PageSizeUsed = [int]$walStats.PageSize
            $script:WalStatus = 'test-only'
            $script:ExtraJson = '"walFrames":' + $walStats.Frames + ',"walCommitted":0,"walApplied":0,"walPageCount":0'
            Write-ResultAndExit -Ok $true -ErrorText ''
        }

        $aes = [System.Security.Cryptography.Aes]::Create()
        $aes.Mode = [System.Security.Cryptography.CipherMode]::CBC
        $aes.Padding = [System.Security.Cryptography.PaddingMode]::None
        $aes.KeySize = 256
        $aes.Key = $encKey
        $hmac = [System.Security.Cryptography.HMACSHA512]::new($macKey)
        if ($hmacType -eq 'HMACSHA1') { $hmac = [System.Security.Cryptography.HMACSHA1]::new($macKey) }

        $pageBuf = New-Object byte[] $pageSize
        $macBuf = New-Object byte[] ($pageSize + 4)
        $zeros = New-Object byte[] $reserve
        $pgno = New-Object byte[] 4
        $iv = New-Object byte[] 16
        $rng = $null
        if ($Mode -eq 'encrypt') { $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create() }
        # The main decrypt deliberately does NOT merge the sibling -wal file: the
        # Python layer calls this script once more in walapply mode, so that a
        # WAL problem can never damage an otherwise good decryption.
        if ($Mode -eq 'decrypt') { $script:WalStatus = 'not-merged' }

        $fsOut = [System.IO.File]::Create($OutPath)
        try {
            for ($p = 1; $p -le $script:Pages; $p++) {
                $skip = 0
                $ctLen = $payloadFull
                if ($p -eq 1) { $skip = 16; $ctLen = $payloadFirst }
                Read-Page -Stream $fsIn -Buffer $pageBuf -Size $pageSize

                $ivOffset = $skip + $ctLen
                $macOffset = $ivOffset + $ivSize

                $pgno[0] = [byte]($p -band 0xff)
                $pgno[1] = [byte](($p -shr 8) -band 0xff)
                $pgno[2] = [byte](($p -shr 16) -band 0xff)
                $pgno[3] = [byte](($p -shr 24) -band 0xff)

                if ($Mode -eq 'decrypt') {
                    # verify HMAC over ciphertext || IV || pgno before trusting anything
                    [System.Array]::Copy($pageBuf, $skip, $macBuf, 0, $ctLen)
                    [System.Array]::Copy($pageBuf, $ivOffset, $macBuf, $ctLen, $ivSize)
                    [System.Array]::Copy($pgno, 0, $macBuf, $ctLen + $ivSize, 4)
                    $mac = $hmac.ComputeHash($macBuf, 0, $ctLen + $ivSize + 4)
                    $diff = 0
                    for ($i = 0; $i -lt $hmacSize; $i++) { $diff = $diff -bor ($mac[$i] -bxor $pageBuf[$macOffset + $i]) }
                    if ($diff -ne 0) {
                        throw ('hmac mismatch (wrong key or salt) on page ' + $p)
                    }
                    [System.Array]::Copy($pageBuf, $ivOffset, $iv, 0, $ivSize)
                    $dec = $aes.CreateDecryptor($encKey, $iv)
                    try { $plain = $dec.TransformFinalBlock($pageBuf, $skip, $ctLen) } finally { $dec.Dispose() }
                    if ($p -eq 1) {
                        $fsOut.Write($script:SqliteMagic, 0, 16)
                    }
                    $fsOut.Write($plain, 0, $plain.Length)
                    $fsOut.Write($zeros, 0, $reserve)
                }
                else {
                    if ($rng -ne $null) { $rng.GetBytes($iv) } else { throw 'random generator missing' }
                    $enc = $aes.CreateEncryptor($encKey, $iv)
                    try { $ct = $enc.TransformFinalBlock($pageBuf, $skip, $ctLen) } finally { $enc.Dispose() }
                    if ($p -eq 1) { $fsOut.Write($saltBytes, 0, 16) }
                    $fsOut.Write($ct, 0, $ct.Length)
                    $fsOut.Write($iv, 0, $ivSize)
                    [System.Array]::Copy($ct, 0, $macBuf, 0, $ctLen)
                    [System.Array]::Copy($iv, 0, $macBuf, $ctLen, $ivSize)
                    [System.Array]::Copy($pgno, 0, $macBuf, $ctLen + $ivSize, 4)
                    $mac = $hmac.ComputeHash($macBuf, 0, $ctLen + $ivSize + 4)
                    $fsOut.Write($mac, 0, $hmacSize)
                    $pad = $reserve - $ivSize - $hmacSize
                    if ($pad -gt 0) { $fsOut.Write($zeros, 0, $pad) }
                }
            }
            $fsOut.Flush()
        }
        finally { $fsOut.Dispose() }
        if ($rng -ne $null) { $rng.Dispose() }
        $hmac.Dispose()
        $aes.Dispose()
    }
    finally { $fsIn.Dispose() }
}
catch {
    $errMsg = $_.Exception.Message
}

if ($errMsg -ne '') {
    Write-ResultAndExit -Ok $false -ErrorText $errMsg
}
Write-ResultAndExit -Ok $true -ErrorText ''
