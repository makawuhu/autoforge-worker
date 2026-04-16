import subprocess
import os
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Optional


class AutoForgeWrapper:
    """Wraps the autoforge CLI for use in a serverless environment."""

    def __init__(self, output_dir: Optional[str] = None):
        self.output_dir = output_dir or tempfile.mkdtemp()
        self.results = {}

    def run(
        self,
        input_image: str,
        csv_file: Optional[str] = None,
        json_file: Optional[str] = None,
        pruning_max_colors: int = 8,
        pruning_max_swaps: int = 20,
        layer_height: float = 0.04,
        max_layers: int = 75,
        iterations: int = 2000,
        background_height: Optional[float] = None,
        nozzle_diameter: Optional[float] = None,
        flatforge: bool = False,
        cap_layers: int = 0,
        **kwargs,
    ) -> dict:
        """
        Run autoforge on the given input image.

        Args:
            input_image: URL or local path to input image
            csv_file: Path to CSV filelist (from Hueforge export)
            json_file: Path to personal_library.json from Hueforge
            pruning_max_colors: Max colors after pruning
            pruning_max_swaps: Max filament swaps
            layer_height: Layer thickness in mm
            max_layers: Maximum number of layers
            iterations: Optimization iterations
            flatforge: Enable FlatForge mode for separate STL per color
            cap_layers: Number of transparent cap layers (FlatForge only)
            **kwargs: Additional autoforge CLI args

        Returns:
            dict with output paths and metadata
        """
        cmd = [
            "autoforge",
            "--input_image", input_image,
            "--output_folder", self.output_dir,
            "--pruning_max_colors", str(pruning_max_colors),
            "--pruning_max_swaps", str(pruning_max_swaps),
            "--layer_height", str(layer_height),
            "--max_layers", str(max_layers),
            "--iterations", str(iterations),
        ]

        if background_height is not None:
            cmd.extend(["--background_height", str(background_height)])
        if nozzle_diameter is not None:
            cmd.extend(["--nozzle_diameter", str(nozzle_diameter)])
        if csv_file:
            cmd.extend(["--csv_file", csv_file])
        if json_file:
            cmd.extend(["--json_file", json_file])
        if flatforge:
            cmd.append("--flatforge")
            if cap_layers > 0:
                cmd.extend(["--cap_layers", str(cap_layers)])
        if kwargs:
            for k, v in kwargs.items():
                cmd.extend([f"--{k}", str(v)])

        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"autoforge failed: {result.stderr}")

        return self._collect_outputs(flatforge)

    def _collect_outputs(self, flatforge: bool) -> dict:
        output_path = Path(self.output_dir)
        outputs = {}

        # DEBUG: log what files exist in output dir
        import sys
        existing = [f.name for f in output_path.iterdir()]
        print(f"[DEBUG _collect_outputs] output_dir={self.output_dir} files={existing}", flush=True)

        if flatforge:
            # FlatForge mode: separate STL per material
            stl_files = list(output_path.glob("*.stl"))
            outputs["stl_files"] = [str(f) for f in stl_files]
            outputs["stl_count"] = len(stl_files)
        else:
            # Traditional mode: single STL
            stl = output_path / "final_model.stl"
            if stl.exists():
                outputs["stl"] = str(stl)
            outputs["stl_files"] = []  # traditional mode uses 'stl' not 'stl_files'

        # Always generated
        composite = output_path / "final_model.png"
        if composite.exists():
            outputs["composite_image"] = str(composite)

        swap_instructions = output_path / "swap_instructions.txt"
        print(f"[DEBUG _collect_outputs] swap_instructions path={swap_instructions} exists={swap_instructions.exists()}", flush=True)
        
        # Read content directly, handle encoding
        try:
            raw_content = swap_instructions.read_bytes()
            print(f"[DEBUG _collect_outputs] raw bytes length={len(raw_content)}", flush=True)
            content = raw_content.decode('utf-8', errors='replace')
            print(f"[DEBUG _collect_outputs] content decoded: {repr(content[:100])}", flush=True)
        except Exception as e:
            print(f"[DEBUG _collect_outputs] Failed to read swap_instructions: {e}", flush=True)
            content = ""
        
        if content.strip():
            outputs["swap_instructions"] = str(swap_instructions)
            lines = content.strip().split("\n")
            # Material swaps give us the count
            swap_lines = [l for l in lines if "swap to" in l]
            print(f"[DEBUG _collect_outputs] swap_lines={swap_lines}", flush=True)
            if swap_lines:
                materials = set(l.split("swap to ")[1].split()[0] for l in swap_lines)
                outputs["material_count"] = len(materials) + 1  # +1 for starting material
            else:
                outputs["material_count"] = 1
            print(f"[DEBUG _collect_outputs] material_count={outputs['material_count']}", flush=True)
            # Layer count from last "At layer #N" line
            layer_lines = [l for l in lines if "At layer #" in l]
            if layer_lines:
                last = layer_lines[-1]
                layer_str = last.split("At layer #")[1].split()[0]
                outputs["layer_count"] = int(layer_str) + 1
            else:
                outputs["layer_count"] = 0
            print(f"[DEBUG _collect_outputs] layer_count={outputs['layer_count']}", flush=True)
        else:
            outputs["material_count"] = 0
            outputs["layer_count"] = 0
            print(f"[DEBUG _collect_outputs] swap_instructions empty or missing - setting counts to 0", flush=True)

        hfp = output_path / "project_file.hfp"
        if hfp.exists():
            outputs["hueforge_project"] = str(hfp)

        # Voxel dimensions from PNG
        if composite.exists():
            try:
                from PIL import Image
                img = Image.open(composite)
                outputs["voxel_dimensions"] = {"width": img.width, "height": img.height}
            except Exception as e:
                print(f"[DEBUG _collect_outputs] PIL error: {e}", flush=True)

        outputs["output_folder"] = self.output_dir
        return outputs

    def package_results(self, flatforge: bool = False) -> str:
        """Create a zip of all output files."""
        zip_path = os.path.join(self.output_dir, "autoforge_outputs.zip")
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in Path(self.output_dir).iterdir():
                if f.name == "autoforge_outputs.zip":
                    continue
                zf.write(f, f.name)
        return zip_path
