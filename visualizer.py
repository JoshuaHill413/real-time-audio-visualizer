import pygame
import numpy as np
import sounddevice as sd
import os
import sys
import math
import time
import random

# ---------------------------------------------------------------------------
# Audio capture
# ---------------------------------------------------------------------------
SAMPLE_RATE = 44100
BLOCK_SIZE = 1024
N_FREQ_BINS = 24  # each particle "listens" to one of these

latest_bass = 0.0
latest_mid = 0.0
latest_treble = 0.0
latest_bars = np.zeros(N_FREQ_BINS)
smoothed_bass = 0.0
smoothed_treble = 0.0
bass_history = [0.0] * 10


def audio_callback(indata, frames, time_info, status):
    global latest_bass, latest_mid, latest_treble, latest_bars
    samples = indata.mean(axis=1)
    fft_result = np.abs(np.fft.rfft(samples))
    freqs = np.fft.rfftfreq(len(samples), d=1.0 / SAMPLE_RATE)

    latest_bass = fft_result[(freqs >= 20) & (freqs < 250)].mean()
    latest_mid = fft_result[(freqs >= 250) & (freqs < 2000)].mean()
    latest_treble = fft_result[(freqs >= 2000) & (freqs < 8000)].mean()

    trimmed = fft_result[5:]
    chunk_size = len(trimmed) // N_FREQ_BINS
    if chunk_size > 0:
        raw_bars = np.array([
            trimmed[i * chunk_size:(i + 1) * chunk_size].mean()
            for i in range(N_FREQ_BINS)
        ])
        latest_bars = np.log1p(raw_bars)


# ---------------------------------------------------------------------------
# Color -- rust / burnt-orange palette
# ---------------------------------------------------------------------------
def rust_color(intensity=0.5):
    intensity = max(0.0, min(1.0, intensity))
    hue = 14
    sat = int(75 - 20 * intensity)
    val = int(18 + 55 * intensity)
    color = pygame.Color(0, 0, 0)
    color.hsva = (hue, max(0, min(100, sat)), max(0, min(100, val)), 100)
    return (color.r, color.g, color.b)


# ---------------------------------------------------------------------------
# Particle field -- generated once, positions never change after this
# ---------------------------------------------------------------------------
NUM_PARTICLES = 400
FIELD_SIZE = 500
MIN_DEPTH = 80
MAX_DEPTH = 900
RECYCLE_THRESHOLD = 30  # respawn a particle once it's this close to (or behind) the camera


class Particle:
    def __init__(self, freq_bin_count):
        self.x = random.uniform(-FIELD_SIZE, FIELD_SIZE)
        self.y = random.uniform(-FIELD_SIZE, FIELD_SIZE)
        self.z = random.uniform(MIN_DEPTH, MAX_DEPTH)
        self.freq_bin = random.randrange(freq_bin_count)

    def respawn_ahead(self, camera_z, freq_bin_count):
        """Called once this particle has passed the camera -- put it back
        out at the far edge of the tunnel with a fresh position, like a
        new star appearing ahead as you keep flying forward."""
        self.x = random.uniform(-FIELD_SIZE, FIELD_SIZE)
        self.y = random.uniform(-FIELD_SIZE, FIELD_SIZE)
        self.z = camera_z + MAX_DEPTH
        self.freq_bin = random.randrange(freq_bin_count)


particles = [Particle(N_FREQ_BINS) for _ in range(NUM_PARTICLES)]


def build_connections(particles, max_dist, max_per_particle=2):
    """Precompute a sparse set of (i, j) index pairs for particles close to
    each other in 3D space. Capped per-particle so it reads as a delicate
    constellation, not a dense mesh."""
    pairs = set()
    for i, p1 in enumerate(particles):
        candidates = []
        for j, p2 in enumerate(particles):
            if i == j:
                continue
            d = math.dist((p1.x, p1.y, p1.z), (p2.x, p2.y, p2.z))
            if d < max_dist:
                candidates.append((d, j))
        candidates.sort()
        for _, j in candidates[:max_per_particle]:
            pairs.add(tuple(sorted((i, j))))
    return pairs


def connections_for(idx, particles, max_dist, max_per_particle=2):
    """Nearest-neighbor pairs for a single particle -- used to refresh its
    web links right after it respawns somewhere new."""
    p1 = particles[idx]
    candidates = []
    for j, p2 in enumerate(particles):
        if j == idx:
            continue
        d = math.dist((p1.x, p1.y, p1.z), (p2.x, p2.y, p2.z))
        if d < max_dist:
            candidates.append((d, j))
    candidates.sort()
    return [tuple(sorted((idx, j))) for _, j in candidates[:max_per_particle]]


CONNECTION_MAX_DIST = 150
CONNECTIONS = build_connections(particles, max_dist=CONNECTION_MAX_DIST, max_per_particle=2)


def refresh_connections(idx, particles):
    """Drop this particle's old links and rebuild fresh ones against its
    new position -- call right after recycling a particle."""
    CONNECTIONS.difference_update({pair for pair in CONNECTIONS if idx in pair})
    CONNECTIONS.update(connections_for(idx, particles, CONNECTION_MAX_DIST, max_per_particle=2))


# ---------------------------------------------------------------------------
# Perspective projection
# ---------------------------------------------------------------------------
def project_particle(px, py, pz, camera_yaw, camera_pitch, camera_roll, focal_length, screen_center):
    """Rotate a fixed point by the camera's roll, yaw, and pitch, then
    perspective-project it to screen space. Returns None if it ends up
    behind the camera (rotated depth <= 0). `pz` should already be the
    particle's depth relative to the camera (world z minus camera_z)."""
    # roll: spins x/y around the flight axis -- this is what makes the
    # whole tunnel appear to spiral as the camera moves forward through it
    cosr, sinr = math.cos(camera_roll), math.sin(camera_roll)
    xr = px * cosr - py * sinr
    yr = px * sinr + py * cosr

    cosy, siny = math.cos(camera_yaw), math.sin(camera_yaw)
    x1 = xr * cosy - pz * siny
    z1 = xr * siny + pz * cosy

    cosp, sinp = math.cos(camera_pitch), math.sin(camera_pitch)
    y1 = yr * cosp - z1 * sinp
    z2 = yr * sinp + z1 * cosp

    if z2 <= 1:
        return None

    scale = focal_length / z2
    sx = screen_center[0] + x1 * scale
    sy = screen_center[1] + y1 * scale
    return sx, sy, scale


# ---------------------------------------------------------------------------
# Pygame setup
# ---------------------------------------------------------------------------
def find_input_device(keywords):
    """Search connected devices for the first name match (case-insensitive)
    that actually has input channels, checking each keyword in order."""
    devices = sd.query_devices()
    for keyword in keywords:
        for i, d in enumerate(devices):
            if keyword.lower() in d['name'].lower() and d['max_input_channels'] > 0:
                return i, d
    return None, None


device_index, device_info = find_input_device(['aggregate', 'blackhole'])

if device_info is None:
    print("1. No Aggregate/BlackHole device found -- falling back to default input.")
    device_index = None
    device_info = sd.query_devices(kind='input')
else:
    print(f"1. Using input device: {device_info['name']} (index {device_index})")

input_channels = min(2, device_info['max_input_channels']) if device_info['max_input_channels'] >= 1 else 1

stream = sd.InputStream(
    device=device_index,
    channels=input_channels,
    samplerate=SAMPLE_RATE,
    blocksize=BLOCK_SIZE,
    callback=audio_callback,
)
print("2. Starting audio stream...")
stream.start()

print("3. Initializing pygame...")
pygame.display.init()
os.environ['SDL_VIDEODRIVER'] = 'cocoa'
width, height = 500, 500
screen = pygame.display.set_mode((width, height))
pygame.display.set_caption("Particle Field")
clock = pygame.time.Clock()
screen_center = (width // 2, height // 2)

FOCAL_LENGTH = 350
BG_COLOR = (10, 7, 5)

print("4. Starting main loop...")
running = True
start_time = time.time()
last_elapsed = 0.0
camera_yaw = 0.0
camera_pitch = 0.0
camera_z = 0.0       # how far the camera has flown forward
camera_roll = 0.0    # spiral twist, accumulates over the song
kick = 0.0           # brief extra pitch punch on a bass hit, decays each frame

# camera "cuts": on a strong hit, swing to a new angle bias and hold it
bias_yaw = 0.0
bias_pitch = 0.0
target_bias_yaw = 0.0
target_bias_pitch = 0.0
last_cut_time = -999.0
CUT_COOLDOWN = 3.0  # minimum seconds between cuts

FORWARD_BASE_SPEED = 40.0     # world units/sec at rest
FORWARD_ENERGY_SPEED = 140.0  # extra speed added at full energy
ROLL_BASE_SPEED = 0.05        # radians/sec at rest
ROLL_ENERGY_SPEED = 0.35      # extra roll speed added at full energy

while running:
    for event in pygame.event.get():
        if event.type == pygame.QUIT:
            running = False
        elif event.type == pygame.KEYDOWN:
            if event.key in (pygame.K_ESCAPE, pygame.K_DELETE):
                running = False

    elapsed = time.time() - start_time
    dt = max(0.0, elapsed - last_elapsed)
    last_elapsed = elapsed

    # ---- smoothing + hit detection ----
    smoothed_bass = smoothed_bass * 0.85 + latest_bass * 0.15
    smoothed_treble = smoothed_treble * 0.85 + latest_treble * 0.15

    bass_history.append(smoothed_bass)
    bass_history.pop(0)
    avg_recent_bass = sum(bass_history[:-1]) / len(bass_history[:-1])
    bass_hit = smoothed_bass > 8 and smoothed_bass > avg_recent_bass * 1.4
    big_hit = smoothed_bass > 8 and smoothed_bass > avg_recent_bass * 2.0  # a noticeably stronger hit

    kick = 1.0 if bass_hit else kick * 0.9

    # ---- overall energy, used to drive forward speed, spiral speed, and cuts ----
    energy = min(1.0, (smoothed_bass + latest_mid + smoothed_treble) / 3 * 15)

    # ---- forward flight + spiral roll, both speed up with the music ----
    forward_speed = FORWARD_BASE_SPEED + FORWARD_ENERGY_SPEED * energy
    camera_z += forward_speed * dt

    roll_speed = ROLL_BASE_SPEED + ROLL_ENERGY_SPEED * energy
    camera_roll += roll_speed * dt

    # ---- camera cuts: on a strong hit (with a cooldown), swing to a new angle ----
    if big_hit and (elapsed - last_cut_time) > CUT_COOLDOWN:
        target_bias_yaw = random.uniform(-0.5, 0.5)
        target_bias_pitch = random.uniform(-0.35, 0.35)
        last_cut_time = elapsed

    bias_yaw += (target_bias_yaw - bias_yaw) * 0.12
    bias_pitch += (target_bias_pitch - bias_pitch) * 0.12

    # ---- camera target angles: yaw rotates continuously so it never stalls
    # at a turning point; pitch oscillates (a full pitch rotation would
    # flip the view upside down); both get the cut bias added on top ----
    target_yaw = elapsed * 0.035 + smoothed_treble * 0.01 + bias_yaw
    target_pitch = (
        math.sin(elapsed * 0.07) * 0.12
        + math.sin(elapsed * 0.13 + 1.7) * 0.06
        + smoothed_bass * 0.008
        + kick * 0.08
        + bias_pitch
    )
    target_pitch = max(-0.9, min(0.9, target_pitch))

    # ease toward the target instead of snapping -- keeps the motion smooth
    camera_yaw += (target_yaw - camera_yaw) * 0.04
    camera_pitch += (target_pitch - camera_pitch) * 0.04

    # ---- normalized per-bin values, for per-particle brightness ----
    bars_max = max(latest_bars.max(), 1e-6)
    bars_norm = latest_bars / bars_max

    # ---- recycle any particle the camera has flown past ----
    for idx, p in enumerate(particles):
        if p.z - camera_z < RECYCLE_THRESHOLD:
            p.respawn_ahead(camera_z, N_FREQ_BINS)
            refresh_connections(idx, particles)

    screen.fill(BG_COLOR)

    # draw back-to-front so nearer particles are layered on top
    projected = []
    visible = {}  # particle index -> (sx, sy, scale), for connection lookups
    for idx, p in enumerate(particles):
        depth = p.z - camera_z
        result = project_particle(p.x, p.y, depth, camera_yaw, camera_pitch, camera_roll, FOCAL_LENGTH, screen_center)
        if result is None:
            continue
        sx, sy, scale = result
        if not (-10 <= sx < width + 10 and -10 <= sy < height + 10):
            continue
        projected.append((scale, sx, sy, p))
        visible[idx] = (sx, sy, scale)

    # ---- constellation lines: faint, pulse with the two endpoints' audio bins ----
    for i, j in CONNECTIONS:
        if i not in visible or j not in visible:
            continue
        sx1, sy1, scale1 = visible[i]
        sx2, sy2, scale2 = visible[j]
        bin_brightness = (bars_norm[particles[i].freq_bin] + bars_norm[particles[j].freq_bin]) / 2
        depth_brightness = min(1.0, (scale1 + scale2) / 2 / 2.5)
        line_brightness = 0.08 + 0.15 * depth_brightness + 0.35 * bin_brightness
        line_color = rust_color(line_brightness)
        pygame.draw.line(screen, line_color, (int(sx1), int(sy1)), (int(sx2), int(sy2)), 1)

    projected.sort(key=lambda item: item[0])  # smallest scale (farthest) first

    for scale, sx, sy, p in projected:
        size = max(1, int(scale * 3))
        depth_brightness = min(1.0, scale / 2.5)
        bin_brightness = bars_norm[p.freq_bin]
        brightness = 0.2 + 0.35 * depth_brightness + 0.55 * bin_brightness
        color = rust_color(brightness)
        pygame.draw.circle(screen, color, (int(sx), int(sy)), size)

    pygame.display.flip()
    clock.tick(60)

print("5. Loop ended. Cleaning up...")
stream.stop()
stream.close()
pygame.quit()
sys.exit()