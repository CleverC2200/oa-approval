#!/usr/bin/env bash
set -euo pipefail
if [ "$#" -ne 1 ]; then
    printf 'Usage: %s NEW_EVIDENCE_DIRECTORY\n' "$0" >&2
    exit 2
fi
project="$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"
cd "$project"
evidence="$(bash .codex/python.sh -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).expanduser().absolute())' "$1")"
mkdir -p -- "$(dirname "$evidence")"
mkdir -m 700 "$evidence"
scratch="$(mktemp -d "${TMPDIR:-/tmp}/oa-material-proof.XXXXXX")"
cleanup() {
    result=$?
    trap - EXIT
    rm -f -- "$scratch/fixture.pdf"
    if rmdir -- "$scratch"; then
        printf '{"scratch_removed":true,"exit_code":%s}\n' "$result" > "$evidence/cleanup.json"
    else
        printf '{"scratch_removed":false,"exit_code":%s}\n' "$result" > "$evidence/cleanup.json"
        result=1
    fi
    exit "$result"
}
trap cleanup EXIT
run() {
    label="$1"
    shift
    printf '%s\t' "$label" >> "$evidence/commands.txt"
    printf '%q ' "$@" >> "$evidence/commands.txt"
    printf '\n' >> "$evidence/commands.txt"
    if "$@" > "$evidence/$label.stdout" 2> "$evidence/$label.stderr"; then
        result=0
    else
        result=$?
    fi
    printf '%s\n' "$result" > "$evidence/$label.exit-code"
    if [ "$result" -ne 0 ]; then
        cat "$evidence/$label.stderr" >&2
        return "$result"
    fi
}
run doctor bash .codex/python.sh -m apps.oa --help
git rev-parse HEAD > "$evidence/head.txt"
git for-each-ref --format='%(refname) %(objectname)' > "$evidence/refs-before.txt"
mkdir "$evidence/inputs"
bash .codex/python.sh - "$scratch" "$evidence" <<'PY'
from pathlib import Path
import hashlib,json,shutil,sys
import fitz
scratch,evidence=map(Path,sys.argv[1:])
with fitz.open() as document:
    document.new_page().insert_text((30,40),'OA verification fixture 12.50')
    document.save(scratch/'fixture.pdf')
source=evidence/'inputs/fixture.pdf'
shutil.copyfile(scratch/'fixture.pdf',source)
(evidence/'source-before.sha256').write_text(hashlib.sha256(source.read_bytes()).hexdigest())
(evidence/'inputs/resources.json').write_text(json.dumps({'resources':[
    {'resource_id':identity,'path':str(source)} for identity in ['same-a','same-b']]}))
PY
run prepare-file bash .codex/python.sh -m apps.oa materials prepare --file "$evidence/inputs/fixture.pdf" --output-dir "$evidence/prepared-file"
run prepare-manifest bash .codex/python.sh -m apps.oa.materials prepare --manifest "$evidence/inputs/resources.json" --output-dir "$evidence/prepared-manifest"
run read-json bash .codex/python.sh -m apps.oa materials read "$evidence/prepared-file/material.json" --format json
run read-text bash .codex/python.sh -m apps.oa.materials read "$evidence/prepared-file/material.json"
run read-outline bash .codex/python.sh -m apps.oa materials read "$evidence/prepared-manifest/material.json" --view outline --format json
git for-each-ref --format='%(refname) %(objectname)' > "$evidence/refs-after.txt"
bash .codex/python.sh - "$evidence" <<'PY'
from pathlib import Path
import hashlib,json,sys
evidence=Path(sys.argv[1])
def saved(name):return json.loads((evidence/(name+'.stdout')).read_text())
one,two=saved('prepare-file'),saved('prepare-manifest')
assert one['status']==two['status']=='complete'
assert (one['resources'],one['images'])==(1,1)
assert (two['resources'],two['images'])==(2,2)
material=json.loads(Path(two['material_file']).read_text())
identities=[r['resource_id'] for r in material['resources']]
assert identities==['same-a','same-b']
marker='OA verification fixture 12.50'
assert marker in saved('read-json')['text']
assert marker in (evidence/'read-text.stdout').read_text()
outline=saved('read-outline')['outline']
assert isinstance(outline,dict) and marker not in json.dumps(outline)
assert hashlib.sha256((evidence/'inputs/fixture.pdf').read_bytes()).hexdigest()==(evidence/'source-before.sha256').read_text()
assert (evidence/'refs-before.txt').read_bytes()==(evidence/'refs-after.txt').read_bytes()
proof={'status':'passed','feature':'local-materials','prepare_entries':['--file','--manifest'],
       'read_entries':['text','json','outline'],'resource_ids':identities,'original_unchanged':True,
       'git_refs_unchanged':True,'evidence':str(evidence),
       'scope':'synthetic single-page PDF and resource identity; no live service or model acceptance'}
(evidence/'proof.json').write_text(json.dumps(proof,ensure_ascii=False,indent=2))
print(json.dumps(proof,ensure_ascii=False))
PY
