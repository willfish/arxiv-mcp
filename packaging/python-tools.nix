{
  lib,
  stdenvNoCC,
  python3,
  fetchurl,
  makeWrapper,
  curl,
}:
let
  model2vec = python3.pkgs.buildPythonPackage {
    pname = "model2vec";
    version = "0.9.0";
    format = "wheel";
    src = fetchurl {
      url = "https://files.pythonhosted.org/packages/af/ea/80246465cafa36a6c8c8ac767778423940e5b826fa57c10ebd957b570c1c/model2vec-0.9.0-py3-none-any.whl";
      hash = "sha256-i88yWNVmhnhznBMialYtOe6uuPysmxQrxKzu+IANW50=";
    };
    dependencies = with python3.pkgs; [
      jinja2
      joblib
      numpy
      safetensors
      tokenizers
      tqdm
      huggingface-hub
    ];
    pythonImportsCheck = [ "model2vec" ];
  };
  python = python3.withPackages (p: [
    p.pyarrow
    p.faiss
    model2vec
  ]);
in
stdenvNoCC.mkDerivation {
  pname = "arxiv-library";
  version = "0.1.0";
  src = lib.cleanSourceWith {
    src = ../python;
    filter =
      path: type:
      (type == "directory" && builtins.baseNameOf path != "__pycache__")
      || (type == "regular" && (lib.hasSuffix ".py" path || lib.hasSuffix ".json" path));
  };
  nativeBuildInputs = [
    makeWrapper
    python
  ];
  doCheck = true;
  checkPhase = ''
    runHook preCheck
    export HOME="$TMPDIR" OPENBLAS_NUM_THREADS=2 OMP_NUM_THREADS=2 RAYON_NUM_THREADS=2
    python -Werror::ResourceWarning -m unittest discover -v
    runHook postCheck
  '';
  installPhase = ''
    runHook preInstall
    mkdir -p "$out/lib/arxiv-library" "$out/bin"
    cp audit.py snapshot.py refresh.py prepare.py download.py library.py ingest.py embeddings.py build_index.py \
      model-lock.json manifest.json "$out/lib/arxiv-library/"
    for pair in refresh:refresh snapshot:snapshot audit:audit prepare:prepare download:download ingest:ingest embeddings:embeddings index:build_index; do
      name="''${pair%%:*}"
      script="''${pair#*:}"
      makeWrapper ${python}/bin/python "$out/bin/arxiv-library-$name" \
        --add-flags "$out/lib/arxiv-library/$script.py" \
        --prefix PATH : ${lib.makeBinPath [ curl ]} \
        --set OPENBLAS_NUM_THREADS 2 --set OMP_NUM_THREADS 2 --set RAYON_NUM_THREADS 2
    done
    runHook postInstall
  '';
  passthru = { inherit python; };
  meta = {
    description = "Verified arXiv ingestion, incremental refresh, audits and snapshots";
    platforms = lib.platforms.linux;
    mainProgram = "arxiv-library-refresh";
  };
}
