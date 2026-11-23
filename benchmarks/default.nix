{ self, pkgs, system }:
let
  src = (import ./source.nix).outPath;
  env = self.lib.mkPackage { inherit system src; dependencies.flask = [ "tests" ]; };
in pkgs.runCommand "drvpy-flask-tests" {} ''
  set -o pipefail
  cp -r ${src} flask
  chmod -R u+w flask
  cd flask
  export HOME="$TMPDIR"
  ${env}/bin/python -m pytest -q tests/test_basic.py | tee tests.log
  grep -oE '[0-9]+ passed' tests.log > "$out"
''
