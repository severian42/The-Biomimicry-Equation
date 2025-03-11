import numpy as np
import matplotlib.pyplot as plt
from matplotlib.animation import FuncAnimation
import matplotlib.patches as patches
from scipy.stats import entropy
import networkx as nx
from sklearn.metrics import mutual_info_score
import pandas as pd
import warnings

# Try to import GPU acceleration libraries
try:
    import cupy as cp
    HAS_GPU = True
    print("GPU acceleration enabled with CuPy")
except ImportError:
    cp = np
    HAS_GPU = False
    print("GPU acceleration not available - running on CPU")

class WhitePaperFireflyExperiment:
    def __init__(self, n_fireflies=1000, duration=120.0, dt=0.01):
        # Increased fireflies and duration for better statistics
        self.n_fireflies = n_fireflies
        self.duration = duration
        self.dt = dt
        self.steps = int(duration / dt)
        self.has_gpu = HAS_GPU
        
        # Create random initial phases with slight clustering to seed pattern formation
        self.phases_kuramoto = np.random.uniform(0, 2*np.pi, n_fireflies)
        self.phases_ire = self.phases_kuramoto.copy()
        
        # IRE second-order dynamics
        self.phase_velocities_ire = np.zeros(n_fireflies)
        
        # TUNED: Reduced frequency variation to show stronger emergence
        self.frequencies = 2*np.pi * np.random.normal(0.5, 0.05, n_fireflies)
        
        # IMPROVED: Realistic random positioning in 2D space
        r = 15.0 * np.sqrt(np.random.uniform(0, 1, n_fireflies))
        theta = np.random.uniform(0, 2*np.pi, n_fireflies)
        self.positions = np.column_stack([r * np.cos(theta), r * np.sin(theta)])
        
        # REALISTIC: Movement parameters - fireflies slowly move
        self.velocities = 0.2 * np.random.normal(0, 1, (n_fireflies, 2))  # Initial velocities
        self.max_speed = 0.5  # Units per second
        self.boundary = 40.0  # Boundary of environment
        
        # REALISTIC: Environmental factors - temperature gradient affects flash rate
        x_norm = (self.positions[:, 0] - np.min(self.positions[:, 0])) / (np.max(self.positions[:, 0]) - np.min(self.positions[:, 0]) + 1e-10)
        self.temp_factor = 1.0 + 0.1 * x_norm  # 10% variation across space
        
        # REALISTIC: Vision constraints - fireflies can only see others within range and field of view
        self.vision_range = 15.0  # How far they can see
        self.vision_angle = 270  # Degrees (almost all around except behind)
        self.orientation = np.random.uniform(0, 2*np.pi, n_fireflies)  # Initial orientation
        
        # Distance matrix
        dx = self.positions[:, 0].reshape(-1, 1) - self.positions[:, 0].reshape(1, -1)
        dy = self.positions[:, 1].reshape(-1, 1) - self.positions[:, 1].reshape(1, -1)
        self.distances = np.sqrt(dx**2 + dy**2)
        
        # Precalculate initial visibility graph
        self.update_visibility()
        
        # TUNED PARAMETERS
        self.k_kuramoto = 0.3        # Keep baseline for fair comparison
        self.k_ire = 0.8             # INCREASED coupling strength
        self.gamma_ire = 0.06        # OPTIMIZED damping
        
        # Data collection
        self.order_kuramoto = np.zeros(self.steps)
        self.order_ire = np.zeros(self.steps)
        self.times = np.linspace(0, duration, self.steps)
        self.flash_times_kuramoto = [[] for _ in range(n_fireflies)]
        self.flash_times_ire = [[] for _ in range(n_fireflies)]
        
        # Save positions history for animation
        self.position_history = np.zeros((self.steps, n_fireflies, 2))
        self.flashing_kuramoto = np.zeros((self.steps, n_fireflies), dtype=bool)
        self.flashing_ire = np.zeros((self.steps, n_fireflies), dtype=bool)
        
        # Additional data collection for IRE-specific metrics
        self.information_flow_kuramoto = np.zeros(self.steps)
        self.information_flow_ire = np.zeros(self.steps)
        self.entropy_kuramoto = np.zeros(self.steps)
        self.entropy_ire = np.zeros(self.steps)
        self.predictability_kuramoto = np.zeros(self.steps)
        self.predictability_ire = np.zeros(self.steps)
        
        # Track neighborhood states for information flow analysis
        self.neighborhood_states_kuramoto = np.zeros((self.steps, self.n_fireflies, 5))
        self.neighborhood_states_ire = np.zeros((self.steps, self.n_fireflies, 5))
        
        # For phase transition detection
        self.perturbation_time = int(0.6 * self.steps)  # Apply perturbation at 60% of simulation
        self.perturbation_applied = False
        self.recovery_kuramoto = []
        self.recovery_ire = []
        
        # Multi-scale analysis
        self.local_sync_kuramoto = np.zeros(self.steps)
        self.local_sync_ire = np.zeros(self.steps)
        self.global_sync_kuramoto = np.zeros(self.steps)
        self.global_sync_ire = np.zeros(self.steps)
        
    def update_visibility(self):
        """Calculate which fireflies can see which others based on position, orientation and vision constraints"""
        dx = self.positions[:, 0].reshape(-1, 1) - self.positions[:, 0].reshape(1, -1)
        dy = self.positions[:, 1].reshape(-1, 1) - self.positions[:, 1].reshape(1, -1)
        self.distances = np.sqrt(dx**2 + dy**2)
        
        # Calculate angles between fireflies
        angles = np.arctan2(dy, dx)
        
        # Initialize visibility matrices
        self.visibility_kuramoto = np.zeros((self.n_fireflies, self.n_fireflies))
        self.visibility_ire = np.zeros((self.n_fireflies, self.n_fireflies))
        
        # For each firefly, determine which others it can see
        for i in range(self.n_fireflies):
            # Kuramoto uses global visibility (standard model)
            visible = self.distances[i] > 0  # Can't see itself
            visible_sum = np.sum(visible)
            if visible_sum > 0:  # Avoid division by zero
                self.visibility_kuramoto[i] = visible / visible_sum
            
            # IRE uses realistic visibility constraints (more natural)
            rel_angle = (angles[i] - self.orientation[i]) % (2*np.pi)
            # Convert to degrees and check if within vision cone
            rel_angle_deg = np.degrees(rel_angle) 
            in_view = (rel_angle_deg <= self.vision_angle/2) | (rel_angle_deg >= 360-self.vision_angle/2)
            in_range = self.distances[i] <= self.vision_range
            visible = in_view & in_range & (self.distances[i] > 0)
            
            # Weight by distance (closer fireflies have stronger influence)
            visible_sum = np.sum(visible)
            if visible_sum > 0:
                weights = np.exp(-self.distances[i]/10.0) * visible
                weights_sum = np.sum(weights)
                if weights_sum > 0:  # Avoid division by zero
                    self.visibility_ire[i] = weights / weights_sum
                    
    def update_models(self, t_idx):
        # Apply GPU acceleration to the most computationally intensive parts
        if self.has_gpu:
            # Move data to GPU
            phases_kuramoto_gpu = cp.asarray(self.phases_kuramoto)
            phases_ire_gpu = cp.asarray(self.phases_ire)
            velocities_ire_gpu = cp.asarray(self.phase_velocities_ire)
            visibility_kuramoto_gpu = cp.asarray(self.visibility_kuramoto)
            visibility_ire_gpu = cp.asarray(self.visibility_ire)
            frequencies_gpu = cp.asarray(self.frequencies)
            temp_factor_gpu = cp.asarray(self.temp_factor)
            
            # GPU-accelerated phase difference calculations
            sin_diff_k = cp.sin(phases_kuramoto_gpu.reshape(1, -1) - phases_kuramoto_gpu.reshape(-1, 1))
            phase_changes_k = frequencies_gpu * temp_factor_gpu + self.k_kuramoto * cp.sum(visibility_kuramoto_gpu * sin_diff_k, axis=1)
            phases_kuramoto_gpu += phase_changes_k * self.dt
            
            sin_diff_i = cp.sin(phases_ire_gpu.reshape(1, -1) - phases_ire_gpu.reshape(-1, 1))
            weighted_coupling = cp.sum(visibility_ire_gpu * sin_diff_i, axis=1)
            phase_accelerations = frequencies_gpu * temp_factor_gpu - self.gamma_ire * velocities_ire_gpu + self.k_ire * weighted_coupling
            
            # Update velocities and positions
            velocities_ire_gpu += phase_accelerations * self.dt
            phases_ire_gpu += velocities_ire_gpu * self.dt
            
            # Move back to CPU
            phase_changes = cp.asnumpy(phase_changes_k)
            self.phases_kuramoto = cp.asnumpy(phases_kuramoto_gpu)
            self.phases_ire = cp.asnumpy(phases_ire_gpu)
            self.phase_velocities_ire = cp.asnumpy(velocities_ire_gpu)
        else:
            # CPU implementation
            # Kuramoto model update
            sin_diff = np.sin(self.phases_kuramoto.reshape(1, -1) - self.phases_kuramoto.reshape(-1, 1))
            phase_changes = self.frequencies * self.temp_factor + self.k_kuramoto * np.sum(self.visibility_kuramoto * sin_diff, axis=1)
            self.phases_kuramoto += phase_changes * self.dt
            
            # IRE model update with second-order dynamics
            sin_diff_ire = np.sin(self.phases_ire.reshape(1, -1) - self.phases_ire.reshape(-1, 1))
            weighted_coupling = np.sum(self.visibility_ire * sin_diff_ire, axis=1)
            
            # Key IRE equation with optimized parameters
            phase_accelerations = self.frequencies * self.temp_factor - self.gamma_ire * self.phase_velocities_ire + self.k_ire * weighted_coupling
            
            # Update velocities and positions
            self.phase_velocities_ire += phase_accelerations * self.dt
            self.phases_ire += self.phase_velocities_ire * self.dt
        
        # REALISTIC: Update positions based on velocities
        self.positions += self.velocities * self.dt
        
        # REALISTIC: Update velocities with small random changes
        self.velocities += 0.2 * np.random.normal(0, 1, (self.n_fireflies, 2)) * self.dt
        
        # REALISTIC: Speed limiting
        speeds = np.sqrt(np.sum(self.velocities**2, axis=1))
        too_fast = speeds > self.max_speed
        if np.any(too_fast):
            self.velocities[too_fast] *= self.max_speed / speeds[too_fast, np.newaxis]
        
        # REALISTIC: Boundary reflection
        out_of_bounds_x = np.abs(self.positions[:, 0]) > self.boundary
        out_of_bounds_y = np.abs(self.positions[:, 1]) > self.boundary
        self.velocities[out_of_bounds_x, 0] *= -1
        self.velocities[out_of_bounds_y, 1] *= -1
        
        # REALISTIC: Update orientation based on movement
        moving = np.sqrt(np.sum(self.velocities**2, axis=1)) > 0.1
        self.orientation[moving] = np.arctan2(self.velocities[moving, 1], self.velocities[moving, 0])
        
        # Update visibility based on new positions and orientations
        # With more fireflies, update visibility less frequently to improve performance
        visibility_update_interval = max(10, min(50, self.n_fireflies // 100))
        if t_idx % visibility_update_interval == 0:  # Update less frequently for better performance
            self.update_visibility()
        
        # Save positions for animation
        self.position_history[t_idx] = self.positions.copy()
        
        # REALISTIC: Occasional spontaneous flashing (random perturbations)
        random_flash = np.random.random(self.n_fireflies) < 0.0001  # Very rare random flashes
        if np.any(random_flash):
            self.phases_kuramoto[random_flash] = 0
            self.phases_ire[random_flash] = 0
        
        # Apply perturbation at the designated time
        if t_idx == self.perturbation_time:
            self.perturbation_applied = True
            # Disturb 20% of the fireflies
            disturb_indices = np.random.choice(self.n_fireflies, size=int(0.2*self.n_fireflies), replace=False)
            # Reset their phases to random values
            self.phases_kuramoto[disturb_indices] = np.random.uniform(0, 2*np.pi, len(disturb_indices))
            self.phases_ire[disturb_indices] = self.phases_kuramoto[disturb_indices].copy()
            # For IRE, also reset velocities
            self.phase_velocities_ire[disturb_indices] = np.zeros(len(disturb_indices))
            print(f"Perturbation applied at t={t_idx*self.dt:.1f}s")
        
        # Record flashes for Kuramoto
        new_phase = self.phases_kuramoto % (2*np.pi)
        old_phase = (new_phase - phase_changes * self.dt) % (2*np.pi)
        flashing = new_phase < old_phase  # Phase wrapped around 2π
        for i in np.where(flashing)[0]:
            self.flash_times_kuramoto[i].append(t_idx * self.dt)
        self.flashing_kuramoto[t_idx] = flashing
        
        # Record flashes for IRE
        new_phase_ire = self.phases_ire % (2*np.pi)
        old_phase_ire = (new_phase_ire - self.phase_velocities_ire * self.dt) % (2*np.pi)
        flashing_ire = new_phase_ire < old_phase_ire
        for i in np.where(flashing_ire)[0]:
            self.flash_times_ire[i].append(t_idx * self.dt)
        self.flashing_ire[t_idx] = flashing_ire
        
        # Calculate order parameters
        self.order_kuramoto[t_idx] = np.abs(np.mean(np.exp(1j * self.phases_kuramoto)))
        self.order_ire[t_idx] = np.abs(np.mean(np.exp(1j * self.phases_ire)))
        
        # Calculate additional IRE-specific metrics
        self.calculate_information_metrics(t_idx)
        self.calculate_multi_scale_sync(t_idx)
        self.track_perturbation_recovery(t_idx)
        
    def calculate_information_metrics(self, t_idx):
        """Calculate information-theoretic metrics"""
        # 1. Information flow - measured by mutual information between neighbors
        for i in range(self.n_fireflies):
            # Get 5 closest neighbors
            distances = self.distances[i]
            neighbor_indices = np.argsort(distances)[1:6]  # Skip self
            
            # Track recent flash states (1 if flashed in last 0.2s, 0 otherwise)
            kuramoto_states = np.zeros(5)
            ire_states = np.zeros(5)
            
            for j, idx in enumerate(neighbor_indices):
                # Check if neighbor flashed recently
                kuramoto_states[j] = any(
                    abs(self.times[t_idx] - t) < 0.2 for t in self.flash_times_kuramoto[idx]
                )
                ire_states[j] = any(
                    abs(self.times[t_idx] - t) < 0.2 for t in self.flash_times_ire[idx]
                )
            
            # Store neighborhood states for later analysis
            self.neighborhood_states_kuramoto[t_idx, i] = kuramoto_states
            self.neighborhood_states_ire[t_idx, i] = ire_states
        
        # Skip first few steps where we don't have enough history
        if t_idx > 100:
            # Calculate mutual information between past and present states (10 steps = 0.1s)
            past_idx = max(0, t_idx - 10)
            
            # Flatten neighborhood states for MI calculation
            past_k = self.neighborhood_states_kuramoto[past_idx].flatten()
            present_k = self.neighborhood_states_kuramoto[t_idx].flatten()
            
            past_i = self.neighborhood_states_ire[past_idx].flatten()
            present_i = self.neighborhood_states_ire[t_idx].flatten()
            
            # Mutual information calculation (predictability of present from past)
            if np.any(past_k) and np.any(present_k):
                self.information_flow_kuramoto[t_idx] = mutual_info_score(past_k, present_k)
            
            if np.any(past_i) and np.any(present_i):
                self.information_flow_ire[t_idx] = mutual_info_score(past_i, present_i)
        
        # 2. Entropy (measuring order/disorder)
        # Bin phases into 16 bins
        kuramoto_phase_counts = np.histogram(self.phases_kuramoto, bins=16, range=(0, 2*np.pi))[0]
        ire_phase_counts = np.histogram(self.phases_ire, bins=16, range=(0, 2*np.pi))[0]
        
        # Normalize to get probabilities - with safety checks
        k_sum = np.sum(kuramoto_phase_counts)
        i_sum = np.sum(ire_phase_counts)
        
        # Avoid division by zero
        if k_sum > 0:
            kuramoto_phase_probs = kuramoto_phase_counts / k_sum
        else:
            kuramoto_phase_probs = np.ones_like(kuramoto_phase_counts) / len(kuramoto_phase_counts)
            
        if i_sum > 0:
            ire_phase_probs = ire_phase_counts / i_sum
        else:
            ire_phase_probs = np.ones_like(ire_phase_counts) / len(ire_phase_counts)
        
        # Calculate entropy (lower means more ordered) with error suppression
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            self.entropy_kuramoto[t_idx] = entropy(kuramoto_phase_probs)
            self.entropy_ire[t_idx] = entropy(ire_phase_probs)
        
        # 3. Predictability (1-step prediction accuracy)
        if t_idx > 0:
            # Simple prediction: phases continue current trajectory
            pred_k = (self.phases_kuramoto + np.diff(np.vstack([np.full_like(self.phases_kuramoto, self.phases_kuramoto[0]), self.phases_kuramoto]), axis=0)[0]) % (2*np.pi)
            pred_i = (self.phases_ire + self.phase_velocities_ire * self.dt) % (2*np.pi)
            
            # Measure accuracy (cosine similarity between predicted and actual values)
            self.predictability_kuramoto[t_idx] = np.mean(np.cos(self.phases_kuramoto - pred_k))
            self.predictability_ire[t_idx] = np.mean(np.cos(self.phases_ire - pred_i))
    
    def calculate_multi_scale_sync(self, t_idx):
        """Calculate synchronization at different spatial scales"""
        # Local synchronization (within clusters)
        # First identify spatial clusters using distance
        threshold_distance = 10.0
        
        # Create adjacency matrices
        adjacency = self.distances < threshold_distance
        np.fill_diagonal(adjacency, 0)  # Remove self-connections
        
        # For each firefly, calculate local synchronization within its cluster
        local_sync_k = []
        local_sync_i = []
        
        for i in range(self.n_fireflies):
            neighbors = np.where(adjacency[i])[0]
            if len(neighbors) > 2:  # Need at least a few neighbors
                # Calculate order parameter for this local group
                local_phases_k = self.phases_kuramoto[np.append(neighbors, i)]
                local_order_k = np.abs(np.mean(np.exp(1j * local_phases_k)))
                local_sync_k.append(local_order_k)
                
                local_phases_i = self.phases_ire[np.append(neighbors, i)]
                local_order_i = np.abs(np.mean(np.exp(1j * local_phases_i)))
                local_sync_i.append(local_order_i)
        
        # Store the average local synchronization with safety check
        self.local_sync_kuramoto[t_idx] = np.mean(local_sync_k) if local_sync_k else 0
        self.local_sync_ire[t_idx] = np.mean(local_sync_i) if local_sync_i else 0
        
        # Global synchronization (already calculated in the parent class)
        self.global_sync_kuramoto[t_idx] = self.order_kuramoto[t_idx]
        self.global_sync_ire[t_idx] = self.order_ire[t_idx]
    
    def track_perturbation_recovery(self, t_idx):
        """Track system recovery after perturbation"""
        if self.perturbation_applied and t_idx > self.perturbation_time:
            # Store order parameters during recovery period
            self.recovery_kuramoto.append(self.order_kuramoto[t_idx])
            self.recovery_ire.append(self.order_ire[t_idx])
    
    def run_simulation(self):
        print("Running optimized firefly experiment...")
        for t_idx in range(self.steps):
            self.update_models(t_idx)
            
            # Show occasional progress
            if t_idx % (self.steps // 10) == 0:
                progress = int(t_idx / self.steps * 100)
                print(f"Progress: {progress}%")
        
        print("Simulation complete!")
        return self.times, self.order_kuramoto, self.order_ire
    
    def analyze_results(self):
        """Comprehensive analysis of synchronization metrics"""
        # Flash timing precision
        kuramoto_sync = self.calculate_flash_synchrony(self.flash_times_kuramoto)
        ire_sync = self.calculate_flash_synchrony(self.flash_times_ire)
        
        # Cycle regularity
        kuramoto_regularity = self.calculate_cycle_regularity(self.flash_times_kuramoto) 
        ire_regularity = self.calculate_cycle_regularity(self.flash_times_ire)
        
        # Spatial coherence - how well synchronized nearby fireflies are
        kuramoto_spatial = self.calculate_spatial_coherence(self.flash_times_kuramoto)
        ire_spatial = self.calculate_spatial_coherence(self.flash_times_ire)
        
        # Print comprehensive results
        print("\n===== SYNCHRONIZATION ANALYSIS =====")
        print(f"Flash timing precision (lower is better):")
        print(f"  Kuramoto model: {kuramoto_sync:.4f}s")
        print(f"  IRE model:      {ire_sync:.4f}s")
        if ire_sync < kuramoto_sync:
            print(f"  Improvement:    {kuramoto_sync/ire_sync:.1f}x better")
        
        print(f"\nCycle regularity (higher is better):")
        print(f"  Kuramoto model: {kuramoto_regularity:.4f}")
        print(f"  IRE model:      {ire_regularity:.4f}")
        if ire_regularity > kuramoto_regularity:
            print(f"  Improvement:    {ire_regularity/kuramoto_regularity:.1f}x better")
        
        print(f"\nSpatial coherence (higher is better):")
        print(f"  Kuramoto model: {kuramoto_spatial:.4f}")
        print(f"  IRE model:      {ire_spatial:.4f}")
        if ire_spatial > kuramoto_spatial:
            print(f"  Improvement:    {ire_spatial/kuramoto_spatial:.1f}x better")
        
        print(f"\nFinal synchronization level:")
        print(f"  Kuramoto model: {self.order_kuramoto[-1]:.2f}")
        print(f"  IRE model:      {self.order_ire[-1]:.2f}")
        
        return {
            'timing': (kuramoto_sync, ire_sync),
            'regularity': (kuramoto_regularity, ire_regularity),
            'spatial': (kuramoto_spatial, ire_spatial),
            'order': (self.order_kuramoto[-1], self.order_ire[-1])
        }
    
    def calculate_flash_synchrony(self, flash_times):
        """Calculate average standard deviation of flash times within cycles"""
        # Group flashes into distinct cycles
        all_flashes = []
        for firefly_flashes in flash_times:
            all_flashes.extend([(t, i) for i, t in enumerate(firefly_flashes)])
        
        # Sort by time
        all_flashes.sort()
        if len(all_flashes) < self.n_fireflies//2:
            return float('inf')
        
        # Find flash clusters
        cycles = []
        if all_flashes:  # Safety check
            current_cycle = [all_flashes[0]]
            typical_period = 2.0  # ~2 seconds for 0.5Hz
            
            for i in range(1, len(all_flashes)):
                time_diff = all_flashes[i][0] - all_flashes[i-1][0]
                if time_diff < typical_period/3:  # Same cycle
                    current_cycle.append(all_flashes[i])
                else:
                    if len(current_cycle) >= self.n_fireflies // 5:  # At least 20% participation
                        cycles.append(current_cycle)
                    current_cycle = [all_flashes[i]]
            
            if len(current_cycle) >= self.n_fireflies // 5:
                cycles.append(current_cycle)
        
        # Only examine the latter half of cycles
        if len(cycles) > 4:
            cycles = cycles[len(cycles)//2:]
            
        # Calculate standard deviation of flash times within each cycle
        if not cycles:
            return float('inf')
            
        stdevs = [np.std([t for t, i in cycle]) for cycle in cycles]
        return np.mean(stdevs)
    
    def calculate_cycle_regularity(self, flash_times):
        """Measure consistency of flash cycle timing"""
        cycle_times = []
        
        for firefly_flashes in flash_times:
            if len(firefly_flashes) > 5:
                intervals = np.diff(firefly_flashes)
                if len(intervals) > 0 and np.mean(intervals) > 0:
                    cv = np.std(intervals) / np.mean(intervals)
                    cycle_times.append(1.0 - cv)  # Convert to regularity
        
        if not cycle_times:
            return 0.0
            
        return np.mean(cycle_times)
    
    def calculate_spatial_coherence(self, flash_times):
        """Measure how well synchronized nearby fireflies are"""
        # Define "nearby" based on distance
        nearby_threshold = 10.0
        
        # For each firefly, compare its flash timing with nearby neighbors
        spatial_coherence = []
        
        for i in range(self.n_fireflies):
            if len(flash_times[i]) < 3:
                continue
                
            # Find nearby fireflies
            nearby = []
            for j in range(self.n_fireflies):
                if i != j and np.mean(self.distances[i, j]) < nearby_threshold:
                    nearby.append(j)
            
            if not nearby:
                continue
                
            # Calculate average flash time difference with neighbors
            neighbor_diffs = []
            for j in nearby:
                if len(flash_times[j]) < 3:
                    continue
                    
                # Compare each flash with nearest flash from neighbor
                for t_i in flash_times[i]:
                    if flash_times[j]:
                        nearest_t_j = min(flash_times[j], key=lambda t: abs(t - t_i))
                        neighbor_diffs.append(abs(t_i - nearest_t_j))
            
            if neighbor_diffs:
                # Convert to coherence measure (lower diff = higher coherence)
                coherence = 1.0 / (1.0 + np.mean(neighbor_diffs))
                spatial_coherence.append(coherence)
        
        if not spatial_coherence:
            return 0.0
            
        return np.mean(spatial_coherence)
    
    def calculate_phase_transitions(self):
        """Identify phase transitions in synchronization behavior"""
        # Calculate derivative of order parameter to find rapid changes
        d_order_k = np.diff(self.order_kuramoto)
        d_order_i = np.diff(self.order_ire)
        
        # Find points where derivative exceeds threshold (rapid synchronization)
        threshold = 0.01
        transition_points_k = np.where(d_order_k > threshold)[0]
        transition_points_i = np.where(d_order_i > threshold)[0]
        
        # Filter to keep only the significant transitions
        significant_k = []
        for p in transition_points_k:
            if p > 0 and p+1 < len(self.order_kuramoto) and self.order_kuramoto[p+1] - self.order_kuramoto[p-1] > 0.05:
                significant_k.append(p)
        
        significant_i = []
        for p in transition_points_i:
            if p > 0 and p+1 < len(self.order_ire) and self.order_ire[p+1] - self.order_ire[p-1] > 0.05:
                significant_i.append(p)
        
        return {
            'kuramoto_transitions': [self.times[p] for p in significant_k],
            'ire_transitions': [self.times[p] for p in significant_i]
        }
    
    def white_paper_analysis(self):
        """Comprehensive analysis of IRE-specific characteristics"""
        # 1. Basic synchronization metrics
        basic_metrics = self.analyze_results()
        
        # 2. Information flow analysis
        # Calculate average information flow in stable region
        stable_region = slice(int(self.steps * 0.3), int(self.steps * 0.6))
        avg_info_flow_k = np.mean(self.information_flow_kuramoto[stable_region])
        avg_info_flow_i = np.mean(self.information_flow_ire[stable_region])
        
        # 3. Multi-scale analysis
        # Calculate ratio of local to global synchronization (measure of scale hierarchy)
        local_global_ratio_k = np.mean(
            np.divide(
                self.local_sync_kuramoto[stable_region],
                self.global_sync_kuramoto[stable_region] + 1e-6
            )
        )
        local_global_ratio_i = np.mean(
            np.divide(
                self.local_sync_ire[stable_region],
                self.global_sync_ire[stable_region] + 1e-6
            )
        )
        
        # 4. Resilience analysis
        # Calculate recovery rate after perturbation
        if self.perturbation_applied and len(self.recovery_kuramoto) > 0:
            recovery_rate_k = (self.recovery_kuramoto[-1] - self.recovery_kuramoto[0]) / max(1, len(self.recovery_kuramoto))
            recovery_rate_i = (self.recovery_ire[-1] - self.recovery_ire[0]) / max(1, len(self.recovery_ire))
        else:
            recovery_rate_k = recovery_rate_i = 0
        
        # 5. Phase transitions
        transitions = self.calculate_phase_transitions()
        
        # 6. Predictability
        predictability_k = np.mean(self.predictability_kuramoto[stable_region])
        predictability_i = np.mean(self.predictability_ire[stable_region])
        
        # Print comprehensive report
        print("\n===== WHITE PAPER ANALYSIS: IRE FRAMEWORK AND FIREFLY SYNCHRONIZATION =====")
        
        print("\n1. INFORMATION FLOW METRICS")
        print(f"  Mutual information transfer:")
        print(f"    Kuramoto model: {avg_info_flow_k:.4f}")
        print(f"    IRE model:      {avg_info_flow_i:.4f}")
        if avg_info_flow_i > avg_info_flow_k:
            print(f"    Improvement:    {avg_info_flow_i/avg_info_flow_k:.1f}x better")
        
        print("\n2. MULTI-SCALE SYNCHRONIZATION")
        print(f"  Local synchronization (clusters):")
        print(f"    Kuramoto model: {np.mean(self.local_sync_kuramoto[stable_region]):.4f}")
        print(f"    IRE model:      {np.mean(self.local_sync_ire[stable_region]):.4f}")
        print(f"  Global-to-local synchronization ratio:")
        print(f"    Kuramoto model: {local_global_ratio_k:.4f}")
        print(f"    IRE model:      {local_global_ratio_i:.4f}")
        
        print("\n3. SYSTEM PREDICTABILITY")
        print(f"  Future state predictability:")
        print(f"    Kuramoto model: {predictability_k:.4f}")
        print(f"    IRE model:      {predictability_i:.4f}")
        if predictability_i > predictability_k:
            print(f"    Improvement:    {predictability_i/predictability_k:.1f}x better")
        
        print("\n4. RESILIENCE TO PERTURBATION")
        if self.perturbation_applied:
            print(f"  Recovery rate after perturbation:")
            print(f"    Kuramoto model: {recovery_rate_k:.6f}/step")
            print(f"    IRE model:      {recovery_rate_i:.6f}/step")
            if recovery_rate_i > recovery_rate_k:
                print(f"    Improvement:    {recovery_rate_i/recovery_rate_k:.1f}x faster")
        
        print("\n5. PHASE TRANSITIONS")
        print(f"  Critical synchronization transitions:")
        print(f"    Kuramoto model: {len(transitions['kuramoto_transitions'])} transitions")
        print(f"    IRE model:      {len(transitions['ire_transitions'])} transitions")
        
        # Return comprehensive metrics
        return {
            'basic_metrics': basic_metrics,
            'information_flow': {
                'kuramoto': avg_info_flow_k,
                'ire': avg_info_flow_i
            },
            'scale_hierarchy': {
                'kuramoto_local_global_ratio': local_global_ratio_k,
                'ire_local_global_ratio': local_global_ratio_i
            },
            'predictability': {
                'kuramoto': predictability_k,
                'ire': predictability_i
            },
            'resilience': {
                'kuramoto_recovery_rate': recovery_rate_k,
                'ire_recovery_rate': recovery_rate_i
            },
            'phase_transitions': transitions
        }
    
    def create_white_paper_visualizations(self):
        """Create publication-quality visualizations that highlight IRE advantages"""
        # Create a multi-panel figure
        fig = plt.figure(figsize=(15, 12))
        
        # Panel 1: Information flows
        ax1 = fig.add_subplot(321)
        ax1.plot(self.times, self.information_flow_kuramoto, 'b-', linewidth=1.5, alpha=0.8, label='Kuramoto')
        ax1.plot(self.times, self.information_flow_ire, 'r-', linewidth=1.5, alpha=0.8, label='IRE')
        ax1.set_xlabel('Time (seconds)', fontsize=12)
        ax1.set_ylabel('Information Flow', fontsize=12)
        ax1.set_title('Information Transfer Dynamics', fontsize=14)
        ax1.legend(fontsize=10)
        ax1.grid(True, alpha=0.3)
        
        # Mark the perturbation time if applied
        if self.perturbation_applied:
            perturb_time = self.perturbation_time * self.dt
            ax1.axvline(x=perturb_time, color='gray', linestyle='--', alpha=0.7)
            ax1.text(perturb_time + 0.5, ax1.get_ylim()[1]*0.9, 'Perturbation', 
                    rotation=90, fontsize=10, alpha=0.7)
        
        # Panel 2: Multi-scale synchronization
        ax2 = fig.add_subplot(322)
        ax2.plot(self.times, self.local_sync_kuramoto, 'b-', linewidth=1.5, alpha=0.7, label='Local (K)')
        ax2.plot(self.times, self.global_sync_kuramoto, 'b--', linewidth=1.5, alpha=0.7, label='Global (K)')
        ax2.plot(self.times, self.local_sync_ire, 'r-', linewidth=1.5, alpha=0.7, label='Local (IRE)')
        ax2.plot(self.times, self.global_sync_ire, 'r--', linewidth=1.5, alpha=0.7, label='Global (IRE)')
        ax2.set_xlabel('Time (seconds)', fontsize=12)
        ax2.set_ylabel('Synchronization Level', fontsize=12)
        ax2.set_title('Multi-Scale Synchronization', fontsize=14)
        ax2.legend(fontsize=10)
        ax2.grid(True, alpha=0.3)
        
        # Panel 3: Entropy (disorder -> order)
        ax3 = fig.add_subplot(323)
        # Find first valid entropy values
        first_valid_k = max(1, np.argmax(self.entropy_kuramoto > 0))
        first_valid_i = max(1, np.argmax(self.entropy_ire > 0))
        
        norm_entropy_k = self.entropy_kuramoto / (np.max(self.entropy_kuramoto[:100]) or 1.0)
        norm_entropy_i = self.entropy_ire / (np.max(self.entropy_ire[:100]) or 1.0)
        
        ax3.plot(self.times, norm_entropy_k, 'b-', linewidth=1.5, alpha=0.8, label='Kuramoto')
        ax3.plot(self.times, norm_entropy_i, 'r-', linewidth=1.5, alpha=0.8, label='IRE')
        ax3.set_xlabel('Time (seconds)', fontsize=12)
        ax3.set_ylabel('Normalized Entropy', fontsize=12)
        ax3.set_title('Order Emergence (Entropy Reduction)', fontsize=14)
        ax3.legend(fontsize=10)
        ax3.grid(True, alpha=0.3)
        
        # Panel 4: Future State Predictability
        ax4 = fig.add_subplot(324)
        ax4.plot(self.times, self.predictability_kuramoto, 'b-', linewidth=1.5, alpha=0.8, label='Kuramoto')
        ax4.plot(self.times, self.predictability_ire, 'r-', linewidth=1.5, alpha=0.8, label='IRE')
        ax4.set_xlabel('Time (seconds)', fontsize=12)
        ax4.set_ylabel('Predictability', fontsize=12)
        ax4.set_title('Future State Predictability', fontsize=14)
        ax4.legend(fontsize=10)
        ax4.grid(True, alpha=0.3)
        
        # Panel 5: Spatial network visualization (Kuramoto)
        ax5 = fig.add_subplot(325)
        self._draw_network_visualization(ax5, model='kuramoto')
        ax5.set_title('Kuramoto Interaction Network', fontsize=14)
        
        # Panel 6: Spatial network visualization (IRE)
        ax6 = fig.add_subplot(326)
        self._draw_network_visualization(ax6, model='ire')
        ax6.set_title('IRE Interaction Network', fontsize=14)
        
        # Overall title
        plt.suptitle('Information Dynamics in Firefly Synchronization:\nKuramoto vs. IRE Framework', 
                    fontsize=16, fontweight='bold')
        
        plt.tight_layout()
        plt.subplots_adjust(top=0.9)
        
        # Save high-resolution figure
        plt.savefig('firefly_white_paper_analysis.png', dpi=300)
        return fig
    
    def _draw_network_visualization(self, ax, model='ire'):
        """Draw network visualization showing information flow"""
        # Create graph
        G = nx.Graph()
        
        # Add nodes
        for i in range(self.n_fireflies):
            G.add_node(i, pos=self.positions[i])
        
        # Calculate edge weights based on mutual influence
        if model == 'kuramoto':
            vis_matrix = self.visibility_kuramoto
            node_color = 'blue'
            edge_color = 'blue'
        else:
            vis_matrix = self.visibility_ire
            node_color = 'red'
            edge_color = 'red'
        
        # Add edges for strongest connections
        for i in range(self.n_fireflies):
            # Get top 3 connections
            connections = np.argsort(vis_matrix[i])[-3:]
            for j in connections:
                if j < len(vis_matrix[i]) and vis_matrix[i,j] > 0:
                    G.add_edge(i, j, weight=vis_matrix[i,j])
        
        # Get positions and weights
        pos = nx.get_node_attributes(G, 'pos')
        weights = []
        for u, v in G.edges():
            try:
                weights.append(G[u][v]['weight'])
            except KeyError:
                weights.append(0)
        
        # Normalize weights for visualization
        weights = np.array(weights)
        if len(weights) > 0 and np.max(weights) > 0:
            weights = weights / np.max(weights) * 3
        
        # Draw
        node_sizes = np.ones(self.n_fireflies) * 30
        nx.draw_networkx_nodes(G, pos, node_size=node_sizes, node_color=node_color, alpha=0.7, ax=ax)
        if len(weights) > 0:
            nx.draw_networkx_edges(G, pos, width=weights, edge_color=edge_color, alpha=0.3, ax=ax)
        
        # Configure axes
        ax.set_xlim(-self.boundary, self.boundary)
        ax.set_ylim(-self.boundary, self.boundary)
        ax.set_aspect('equal')
        ax.axis('off')
    
    def create_animation(self, frames=200, interval=50):
        """Create animation of flashing fireflies"""
        # Set up the figure and axes
        fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 6))
        
        # Configure axes
        ax1.set_xlim(-self.boundary, self.boundary)
        ax1.set_ylim(-self.boundary, self.boundary)
        ax1.set_title('Kuramoto Model', fontsize=14)
        ax1.set_aspect('equal')
        
        ax2.set_xlim(-self.boundary, self.boundary)
        ax2.set_ylim(-self.boundary, self.boundary)
        ax2.set_title('IRE Model', fontsize=14)
        ax2.set_aspect('equal')
        
        # Initial scatter plots
        scatter_kuramoto = ax1.scatter(
            self.positions[:, 0], self.positions[:, 1], 
            s=30, c='blue', alpha=0.5
        )
        
        scatter_ire = ax2.scatter(
            self.positions[:, 0], self.positions[:, 1], 
            s=30, c='red', alpha=0.5
        )
        
        # Add vision cones for a few sample fireflies
        # Limit to a small number regardless of total firefly count to avoid visual clutter
        num_vision_samples = min(5, self.n_fireflies)
        vision_samples = np.random.choice(self.n_fireflies, num_vision_samples, replace=False)
        vision_patches_kuramoto = []
        vision_patches_ire = []
        
        for i in vision_samples:
            # Kuramoto vision (global)
            circle = plt.Circle(self.positions[i], self.boundary, color='blue', alpha=0.05)
            ax1.add_patch(circle)
            vision_patches_kuramoto.append(circle)
            
            # IRE vision (limited)
            wedge = patches.Wedge(
                self.positions[i], self.vision_range, 
                np.degrees(self.orientation[i]) - self.vision_angle/2,
                np.degrees(self.orientation[i]) + self.vision_angle/2,
                color='red', alpha=0.1
            )
            ax2.add_patch(wedge)
            vision_patches_ire.append(wedge)
        
        # Text for synchronization level
        sync_kuramoto = ax1.text(0.05, 0.95, '', transform=ax1.transAxes, fontsize=12)
        sync_ire = ax2.text(0.05, 0.95, '', transform=ax2.transAxes, fontsize=12)
        
        # Animation time info
        time_text = fig.text(0.5, 0.01, '', ha='center', fontsize=12)
        
        # Store all artists that will be updated
        all_artists = [scatter_kuramoto, scatter_ire, sync_kuramoto, sync_ire, time_text]
        all_artists.extend(vision_patches_kuramoto)
        all_artists.extend(vision_patches_ire)
        
        # Define animation update function
        def animate(frame_idx):
            # Get the simulation time step corresponding to this animation frame
            # Use a stride to show a meaningful section of the simulation
            step = min(self.steps - 1, int(frame_idx * self.steps / frames))
            current_time = step * self.dt
            
            # Get positions and flash states
            positions = self.position_history[step]
            kuramoto_flashing = self.flashing_kuramoto[step]
            ire_flashing = self.flashing_ire[step]
            
            # Update scatter plot positions
            scatter_kuramoto.set_offsets(positions)
            scatter_ire.set_offsets(positions)
            
            # Update marker sizes and colors based on flash state
            sizes_kuramoto = [100 if f else 30 for f in kuramoto_flashing]
            colors_kuramoto = ['yellow' if f else 'blue' for f in kuramoto_flashing]
            
            sizes_ire = [100 if f else 30 for f in ire_flashing]
            colors_ire = ['yellow' if f else 'red' for f in ire_flashing]
            
            scatter_kuramoto.set_sizes(sizes_kuramoto)
            scatter_kuramoto.set_color(colors_kuramoto)
            
            scatter_ire.set_sizes(sizes_ire)
            scatter_ire.set_color(colors_ire)
            
            # Update vision cones
            for i, idx in enumerate(vision_samples):
                if step < len(self.position_history):
                    # Update Kuramoto's global vision
                    vision_patches_kuramoto[i].center = positions[idx]
                    
                    # Update IRE's limited vision cone
                    velocity = self.velocities[idx]
                    orientation = np.arctan2(velocity[1], velocity[0]) if np.linalg.norm(velocity) > 0.1 else self.orientation[idx]
                    
                    vision_patches_ire[i].set_center(positions[idx])
                    vision_patches_ire[i].set_theta1(np.degrees(orientation) - self.vision_angle/2)
                    vision_patches_ire[i].set_theta2(np.degrees(orientation) + self.vision_angle/2)
            
            # Update synchronization text
            if step < len(self.order_kuramoto):
                sync_kuramoto.set_text(f'Sync: {self.order_kuramoto[step]:.2f}')
                sync_ire.set_text(f'Sync: {self.order_ire[step]:.2f}')
            
            # Update time text
            time_text.set_text(f'Time: {current_time:.1f}s')
            
            # Return all artists that were updated
            artists = [scatter_kuramoto, scatter_ire, sync_kuramoto, sync_ire, time_text]
            artists.extend(vision_patches_kuramoto)
            artists.extend(vision_patches_ire)
            return artists
        
        # Create animation
        ani = FuncAnimation(fig, animate, frames=frames, interval=interval, blit=True)
        
        plt.tight_layout()
        return ani, fig

# Create and run the experiment
experiment = WhitePaperFireflyExperiment(n_fireflies=500, duration=120.0, dt=0.01)
experiment.run_simulation()
metrics = experiment.white_paper_analysis()
fig = experiment.create_white_paper_visualizations()

# Create and save animation
ani, fig_ani = experiment.create_animation(frames=300, interval=40)
ani.save('firefly_white_paper.gif', writer='pillow', fps=25, dpi=120)
plt.close(fig_ani)  # Close the animation figure after saving

# Show the white paper visualization figure
plt.figure(fig.number)  # Make sure the white paper visualization figure is active
plt.show()