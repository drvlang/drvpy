{ pkgs, src, memberPath, env, testArgs }:
let inherit (pkgs) lib;
in pkgs.runCommand "drvpy-test" {
  __contentAddressed = true;
  passthru = { inherit (env) planner; };
} ''
  cp -r ${src} project
  chmod -R u+w project
  cd project/${lib.escapeShellArg memberPath}
  export HOME="$TMPDIR"
  if [[ -x ${env}/bin/pytest ]]; then
    ${env}/bin/python -m pytest ${lib.escapeShellArgs testArgs}
  else
    args=(${lib.escapeShellArgs testArgs})
    if [[ ''${#args[@]} != 0 ]]; then
      ${env}/bin/python -m unittest discover "''${args[@]}"
    else
      tested=false
      members=(.)
      if [[ -e ${env}/nix-support/drvpy-test-roots ]]; then
        mapfile -d "" -t members < ${env}/nix-support/drvpy-test-roots
      fi
      for member in "''${members[@]}"; do
        if (
          cd "$member"
          if [[ -d tests ]]; then args=(-s tests); fi
          ${env}/bin/python -m unittest discover "''${args[@]}"
        ); then
          tested=true
        else
          status=$?
          # unittest returns 5 when a member has no tests to discover.
          if [[ $status != 5 ]]; then exit "$status"; fi
        fi
      done
      if [[ $tested == false ]]; then exit 5; fi
    fi
  fi
  mkdir "$out"
''
