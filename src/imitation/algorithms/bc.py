"""Behavioural Cloning (BC).

Trains policy by applying supervised learning to a fixed dataset of (observation,
action) pairs generated by some expert demonstrator.
"""

import contextlib
from typing import Any, Callable, Iterable, Mapping, Optional, Tuple, Type, Union

import gym
import numpy as np
import torch as th
import tqdm.autonotebook as tqdm
from stable_baselines3.common import policies, utils, vec_env

from imitation.algorithms import base as algo_base
from imitation.data import rollout, types
from imitation.policies import base as policy_base
from imitation.util import logger

from scipy.optimize import linear_sum_assignment
import math


def reconstruct_policy(
    policy_path: str,
    device: Union[th.device, str] = "auto",
) -> policies.BasePolicy:
    """Reconstruct a saved policy.

    Args:
        policy_path: path where `.save_policy()` has been run.
        device: device on which to load the policy.

    Returns:
        policy: policy with reloaded weights.
    """
    policy = th.load(policy_path, map_location=utils.get_device(device))
    assert isinstance(policy, policies.BasePolicy)
    return policy


class ConstantLRSchedule:
    """A callable that returns a constant learning rate."""

    def __init__(self, lr: float = 1e-3):
        """Builds ConstantLRSchedule.

        Args:
            lr: the constant learning rate that calls to this object will return.
        """
        self.lr = lr

    def __call__(self, _):
        """Returns the constant learning rate."""
        return self.lr


class _NoopTqdm:
    """Dummy replacement for tqdm.tqdm() when we don't want a progress bar visible."""

    def close(self):
        pass

    def set_description(self, s):
        pass

    def update(self, n):
        pass


class EpochOrBatchIteratorWithProgress:
    """Wraps DataLoader so that all BC batches can be processed in one for-loop.

    Also uses `tqdm` to show progress in stdout.
    """

    def __init__(
        self,
        data_loader: Iterable[algo_base.TransitionMapping],
        n_epochs: Optional[int] = None,
        n_batches: Optional[int] = None,
        on_epoch_end: Optional[Callable[[], None]] = None,
        on_batch_end: Optional[Callable[[], None]] = None,
        progress_bar_visible: bool = True,
    ):
        """Builds EpochOrBatchIteratorWithProgress.

        Args:
            data_loader: An iterable over data dicts, as used in `BC`.
            n_epochs: The number of epochs to iterate through in one call to
                __iter__. Exactly one of `n_epochs` and `n_batches` should be provided.
            n_batches: The number of batches to iterate through in one call to
                __iter__. Exactly one of `n_epochs` and `n_batches` should be provided.
            on_epoch_end: A callback function without parameters to be called at the
                end of every epoch.
            on_batch_end: A callback function without parameters to be called at the
                end of every batch.
            progress_bar_visible: If True, then show a tqdm progress bar.

        Raises:
            ValueError: If neither or both of `n_epochs` and `n_batches` are non-None.
        """
        if n_epochs is not None and n_batches is None:
            self.use_epochs = True
        elif n_epochs is None and n_batches is not None:
            self.use_epochs = False
        else:
            raise ValueError(
                "Must provide exactly one of `n_epochs` and `n_batches` arguments.",
            )

        self.data_loader = data_loader
        self.n_epochs = n_epochs
        self.n_batches = n_batches
        self.on_epoch_end = on_epoch_end
        self.on_batch_end = on_batch_end
        self.progress_bar_visible = progress_bar_visible

    def __iter__(
        self,
    ) -> Iterable[Tuple[algo_base.TransitionMapping, Mapping[str, Any]]]:
        """Yields batches while updating tqdm display to display progress."""
        samples_so_far = 0
        epoch_num = 0
        batch_num = 0
        batch_suffix = epoch_suffix = ""
        if self.progress_bar_visible:
            if self.use_epochs:
                display = tqdm.tqdm(total=self.n_epochs)
                epoch_suffix = f"/{self.n_epochs}"
            else:  # Use batches.
                display = tqdm.tqdm(total=self.n_batches)
                batch_suffix = f"/{self.n_batches}"
        else:
            display = _NoopTqdm()

        def update_desc():
            display.set_description(
                f"batch: {batch_num}{batch_suffix}  epoch: {epoch_num}{epoch_suffix}",
            )

        with contextlib.closing(display):
            while True:
                update_desc()
                got_data_on_epoch = False
                for batch in self.data_loader:
                    got_data_on_epoch = True
                    batch_num += 1
                    batch_size = len(batch["obs"])
                    assert batch_size > 0
                    samples_so_far += batch_size
                    stats = dict(
                        epoch_num=epoch_num,
                        batch_num=batch_num,
                        samples_so_far=samples_so_far,
                    )
                    yield batch, stats
                    if self.on_batch_end is not None:
                        self.on_batch_end()
                    if not self.use_epochs:
                        update_desc()
                        display.update(1)
                        if batch_num >= self.n_batches:
                            return
                if not got_data_on_epoch:
                    raise AssertionError(
                        f"Data loader returned no data after "
                        f"{batch_num} batches, during epoch "
                        f"{epoch_num} -- did it reset correctly?",
                    )
                epoch_num += 1
                if self.on_epoch_end is not None:
                    self.on_epoch_end()

                if self.use_epochs:
                    update_desc()
                    display.update(1)
                    if epoch_num >= self.n_epochs:
                        return


class BC(algo_base.DemonstrationAlgorithm):
    """Behavioral cloning (BC).

    Recovers a policy via supervised learning from observation-action pairs.
    """

    def __init__(
        self,
        *,
        observation_space: gym.Space,
        action_space: gym.Space,
        policy: Optional[policies.BasePolicy] = None,
        demonstrations: Optional[algo_base.AnyTransitions] = None,
        batch_size: int = 32,
        optimizer_cls: Type[th.optim.Optimizer] = th.optim.Adam,
        optimizer_kwargs: Optional[Mapping[str, Any]] = None,
        ent_weight: float = 1e-3,
        l2_weight: float = 0.0,
        device: Union[str, th.device] = "auto",
        custom_logger: Optional[logger.HierarchicalLogger] = None,
        traj_size_pos_ctrl_pts = None,
        traj_size_yaw_ctrl_pts = None,
        weight_prob=0.01
    ):
        """Builds BC.

        Args:
            observation_space: the observation space of the environment.
            action_space: the action space of the environment.
            policy: a Stable Baselines3 policy; if unspecified,
                defaults to `FeedForward32Policy`.
            demonstrations: Demonstrations from an expert (optional). Transitions
                expressed directly as a `types.TransitionsMinimal` object, a sequence
                of trajectories, or an iterable of transition batches (mappings from
                keywords to arrays containing observations, etc).
            batch_size: The number of samples in each batch of expert data.
            optimizer_cls: optimiser to use for supervised training.
            optimizer_kwargs: keyword arguments, excluding learning rate and
                weight decay, for optimiser construction.
            ent_weight: scaling applied to the policy's entropy regularization.
            l2_weight: scaling applied to the policy's L2 regularization.
            device: name/identity of device to place policy on.
            custom_logger: Where to log to; if None (default), creates a new logger.

        Raises:
            ValueError: If `weight_decay` is specified in `optimizer_kwargs` (use the
                parameter `l2_weight` instead.)
        """
        self.traj_size_pos_ctrl_pts=traj_size_pos_ctrl_pts;
        self.traj_size_yaw_ctrl_pts=traj_size_yaw_ctrl_pts;
        self.weight_prob=weight_prob;
        self.batch_size = batch_size
        super().__init__(
            demonstrations=demonstrations,
            custom_logger=custom_logger,
        )

        if optimizer_kwargs:
            if "weight_decay" in optimizer_kwargs:
                raise ValueError("Use the parameter l2_weight instead of weight_decay.")
        self.tensorboard_step = 0

        self.action_space = action_space
        self.observation_space = observation_space
        self.device = utils.get_device(device)

        if policy is None:
            policy = policy_base.FeedForward32Policy(
                observation_space=observation_space,
                action_space=action_space,
                # Set lr_schedule to max value to force error if policy.optimizer
                # is used by mistake (should use self.optimizer instead).
                lr_schedule=ConstantLRSchedule(th.finfo(th.float32).max),
            )
        self._policy = policy.to(self.device)
        # TODO(adam): make policy mandatory and delete observation/action space params?
        assert self.policy.observation_space == self.observation_space
        assert self.policy.action_space == self.action_space

        optimizer_kwargs = optimizer_kwargs or {}
        self.optimizer = optimizer_cls(
            self.policy.parameters(),
            **optimizer_kwargs,
        )

        self.ent_weight = ent_weight
        self.l2_weight = l2_weight

    @property
    def policy(self) -> policies.BasePolicy:
        return self._policy

    def set_demonstrations(self, demonstrations: algo_base.AnyTransitions) -> None:
        self._demo_data_loader = algo_base.make_data_loader(
            demonstrations,
            self.batch_size,
        )

    def _calculate_loss(
        self,
        obs: Union[th.Tensor, np.ndarray],
        acts: Union[th.Tensor, np.ndarray],
    ) -> Tuple[th.Tensor, Mapping[str, float]]:
        """Calculate the supervised learning loss used to train the behavioral clone.

        Args:
            obs: The observations seen by the expert. If this is a Tensor, then
                gradients are detached first before loss is calculated.
            acts: The actions taken by the expert. If this is a Tensor, then its
                gradients are detached first before loss is calculated.

        Returns:
            loss: The supervised learning loss for the behavioral clone to optimize.
            stats_dict: Statistics about the learning process to be logged.

        """
        obs = th.as_tensor(obs, device=self.device).detach()
        acts = th.as_tensor(acts, device=self.device).detach()

        if isinstance(self.policy, policies.ActorCriticPolicy):
            _, log_prob, entropy = self.policy.evaluate_actions(obs, acts)
            prob_true_act = th.exp(log_prob).mean()
            log_prob = log_prob.mean()
            entropy = entropy.mean()

            l2_norms = [th.sum(th.square(w)) for w in self.policy.parameters()]
            l2_norm = sum(l2_norms) / 2  # divide by 2 to cancel with gradient of square

            ent_loss = -self.ent_weight * entropy
            neglogp = -log_prob
            l2_loss = self.l2_weight * l2_norm
            loss = neglogp + ent_loss + l2_loss

            stats_dict = dict(
                neglogp=neglogp.item(),
                loss=loss.item(),
                entropy=entropy.item(),
                ent_loss=ent_loss.item(),
                prob_true_act=prob_true_act.item(),
                l2_norm=l2_norm.item(),
                l2_loss=l2_loss.item(),
            )

        else:
            pred_acts = self.policy.forward(obs, deterministic=True)
            # print("=====================================PRED ACTS")
            # print("pred_acts.shape= ", pred_acts.shape)
            # print("pred_acts.float()= ", pred_acts.float())
            # print("\n\n\n\n\n=====================================ACTS")
            # print("acts.shape= ", acts.shape)
            # print("acts.float()= ", acts.float())
            # loss = th.nn.MSELoss(reduction='mean')(pred_acts.float(), acts.float())
            ##########################

            used_device=acts.device;

            #Expert --> i
            #Student --> j
            num_of_traj_per_action=list(acts.shape)[1] #acts.shape is [batch size, num_traj_action, size_traj]
            num_of_elements_per_traj=list(acts.shape)[2] #acts.shape is [batch size, num_traj_action, size_traj]
            batch_size=list(acts.shape)[0] #acts.shape is [batch size, num_of_traj_per_action, size_traj]
            
            #### OLD
            # distance_matrix_old= th.zeros(batch_size, num_of_traj_per_action, num_of_traj_per_action); 
            # distance_pos_matrix_old= th.zeros(batch_size, num_of_traj_per_action, num_of_traj_per_action); 

            # for index_batch in range(batch_size):           
            #     for i in range(num_of_traj_per_action):
            #         for j in range(num_of_traj_per_action):

            #             expert_i=acts[index_batch,i,:].float();
            #             expert_pos_i=acts[index_batch,i,0:self.traj_size_pos_ctrl_pts].float();

            #             student_j=pred_acts[index_batch,j,:].float()
            #             student_pos_j=pred_acts[index_batch,j,0:self.traj_size_pos_ctrl_pts].float()

            #             distance_matrix_old[index_batch,i,j]=th.nn.MSELoss(reduction='mean')(expert_i, student_j)
            #             distance_pos_matrix_old[index_batch,i,j]=th.nn.MSELoss(reduction='mean')(expert_pos_i, student_pos_j)
    

            ############################

            # acts[:,:,-1]=2*(th.randint(0, 2, acts[:,:,-1].shape, device=used_device) - 0.5*th.ones(acts[:,:,-1].shape, device=used_device))
            # print(f"acts[:,:,:]=\n{acts[:,:,:]}")
            # print(f"acts[:,:,-1]=\n{acts[:,:,-1]}")


            distance_matrix= th.zeros(batch_size, num_of_traj_per_action, num_of_traj_per_action, device=used_device); 
            distance_pos_matrix= th.zeros(batch_size, num_of_traj_per_action, num_of_traj_per_action, device=used_device); 
            distance_yaw_matrix= th.zeros(batch_size, num_of_traj_per_action, num_of_traj_per_action, device=used_device); 
            distance_time_matrix= th.zeros(batch_size, num_of_traj_per_action, num_of_traj_per_action, device=used_device); 

            for i in range(num_of_traj_per_action):
                for j in range(num_of_traj_per_action):

                    expert_i=       acts[:,i,0:-1].float(); #All the elements but the last one
                    student_j=      pred_acts[:,j,0:-1].float() #All the elements but the last one

                    expert_pos_i=   acts[:,i,0:self.traj_size_pos_ctrl_pts].float();
                    student_pos_j=  pred_acts[:,j,0:self.traj_size_pos_ctrl_pts].float()

                    expert_yaw_i=   acts[:,i,self.traj_size_pos_ctrl_pts:(self.traj_size_pos_ctrl_pts+self.traj_size_yaw_ctrl_pts)].float();
                    student_yaw_j=  pred_acts[:,j,self.traj_size_pos_ctrl_pts:(self.traj_size_pos_ctrl_pts+self.traj_size_yaw_ctrl_pts)].float()

                    expert_time_i=       acts[:,i,-2].float(); #Time
                    student_time_j=      pred_acts[:,j,-2].float() #Time

                    distance_matrix[:,i,j]=th.sum(th.nn.MSELoss(reduction='none')(expert_i, student_j), dim=1)/num_of_elements_per_traj
                    distance_pos_matrix[:,i,j]=th.sum(th.nn.MSELoss(reduction='none')(expert_pos_i, student_pos_j), dim=1)/self.traj_size_pos_ctrl_pts
                    distance_yaw_matrix[:,i,j]=th.sum(th.nn.MSELoss(reduction='none')(expert_yaw_i, student_yaw_j), dim=1)/self.traj_size_yaw_ctrl_pts
                    distance_time_matrix[:,i,j]=th.sum(th.nn.MSELoss(reduction='none')(expert_time_i, student_time_j), dim=0)

            # th.sum(

            # print("Difference=",distance_matrix-distance_matrix_old)
            # print("Difference Pos=",distance_pos_matrix-distance_pos_matrix_old)

            ############################

                        # print(f"Expert {i} = ",expert_pos_i)
            #distance_matrix[:,i,j] is a vector of batch_size elements
            # print("distance_pos_matrix=\n", distance_pos_matrix)


            alpha_matrix=th.ones(batch_size, num_of_traj_per_action, num_of_traj_per_action, device=used_device);
            distance_pos_matrix_numpy=distance_pos_matrix.cpu().detach().numpy();

            if(num_of_traj_per_action>1):

                #Option 1: Solve assignment problem
                alpha_matrix=th.zeros(batch_size, num_of_traj_per_action, num_of_traj_per_action, device=used_device);
                for index_batch in range(batch_size): 
                    cost_matrix=distance_pos_matrix_numpy[index_batch,:,:];
                    map2RealRows=np.array(range(num_of_traj_per_action))
                    map2RealCols=np.array(range(num_of_traj_per_action))

                    rows_to_delete=[]
                    for i in range(num_of_traj_per_action):
                        expert_prob=th.round(acts[index_batch, i, -1]) #this should be either 1 or -1
                        if(expert_prob==-1): 
                            #Delete that row
                            rows_to_delete.append(i)

                    # print(f"Deleting index_batch={index_batch}, rows_to_delete={rows_to_delete}")
                    cost_matrix=np.delete(cost_matrix, rows_to_delete, axis=0)
                    map2RealRows=np.delete(map2RealRows, rows_to_delete, axis=0)

                    row_indexes, col_indexes = linear_sum_assignment(cost_matrix)
                    for row_index, col_index in zip(row_indexes, col_indexes):
                        alpha_matrix[index_batch, map2RealRows[row_index], map2RealCols[col_index]]=1
                        # alpha_matrix[index_batch, row_index, col_index]=1-epsilon

                # print(f"alpha_matrix={alpha_matrix}")
                col_assigned=th.round(th.sum(alpha_matrix, dim=1)); #Example: col_assigned[2,:,:]=[0 0 1 0 1 0] means that the 3rd and 5th columns have been assigned
                col_not_assigned=(~(col_assigned.bool())).float();
                col_assigned=col_assigned.float()

                #Option 3: simply the identity matrix
                # x = th.eye(num_of_traj_per_action)
                # x = x.reshape((1, num_of_traj_per_action, num_of_traj_per_action))
                # alpha_matrix = x.repeat(batch_size, 1, 1)

                # print("alpha_matrix=\n", alpha_matrix)

                #Option 2: Eq.6 of https://arxiv.org/pdf/2110.05113.pdfs
                # epsilon=0.05
                # alpha_matrix=(epsilon/(num_of_traj_per_action-1)) * alpha_matrix;
                # for index_batch in range(batch_size): 
                #    for j in range(num_of_traj_per_action): #for each column (student traj)
                #        # get minimum of the column and assign 1-epsilon to it
                #        # print("distance_pos_matrix[:,j]= ", distance_pos_matrix[:,j])
                #        (min_value, argmin_index_row)=th.min(distance_pos_matrix[index_batch,:,j], dim=0) 
                #        # print("argmin_index_row= ", argmin_index_row)
                #        alpha_matrix[index_batch, argmin_index_row,j]=1-epsilon

            # print("alpha_matrix=\n", alpha_matrix)
            # print(f"col_assigned=\n {col_assigned}")
            # print(f"col_not_assigned=\n {col_not_assigned}")

            # print("distance_pos_matrix=\n", distance_pos_matrix)

            # print(f"===============")
            # print(f"distance_pos_matrix.device={distance_pos_matrix.device}")
            # print(f"alpha_matrix.device={alpha_matrix.device}")
            # print(f"===============")
            # print(f"student_probs.device= {student_probs.device}")
            student_probs=pred_acts[:,:,-1]
            tmp=th.ones(student_probs.shape, device=used_device)
            # print(f"tmp.device= {tmp.device}")
            # print(f"col_assigned.device= {col_assigned.device}")
            #Elementwise mult, see https://stackoverflow.com/questions/53369667/pytorch-element-wise-product-of-vectors-matrices-tensors
            # print("===========================")
            # print("student_probs=\n", student_probs)
            # print("col_assigned=\n", col_assigned)
            # print("uno=\n", col_assigned*th.nn.MSELoss(reduction='none')(student_probs,tmp))
            # print("dos=\n", col_not_assigned*th.nn.MSELoss(reduction='none')(student_probs,-tmp))
            assert (distance_matrix.shape)[0]==(student_probs.shape)[0], f"Wrong shape!, distance_matrix.shape={distance_matrix.shape}, student_probs.shape={student_probs.shape}"
            assert (distance_matrix.shape)[1]==(student_probs.shape)[1], f"Wrong shape!, distance_matrix.shape={distance_matrix.shape}, student_probs.shape={student_probs.shape}"
            assert (distance_matrix.shape)[0]==batch_size, "Wrong shape!"
            assert (distance_matrix.shape)[1]==num_of_traj_per_action, "Wrong shape!"

            #each of the terms below are matrices of shape (batch_size)x(num_of_traj_per_action)

            norm_constant=(1/(batch_size*num_of_traj_per_action))

            pos_yaw_time_loss=norm_constant*th.sum(alpha_matrix*distance_matrix)
            prob_loss=norm_constant*th.sum(col_assigned*th.nn.MSELoss(reduction='none')(student_probs,tmp)) + th.sum(col_not_assigned*th.nn.MSELoss(reduction='none')(student_probs,-tmp)) # This sum has batch_size*num_of_traj_per_action terms


            #For debugging
            pos_loss=norm_constant*th.sum(alpha_matrix*distance_pos_matrix)
            yaw_loss=norm_constant*th.sum(alpha_matrix*distance_yaw_matrix)
            time_loss=norm_constant*th.sum(alpha_matrix*distance_time_matrix)

            loss=pos_yaw_time_loss +self.weight_prob*prob_loss;
            
            # print("loss=\n", loss)

            # print("loss=\n ",loss)

            ##########################
            stats_dict = dict(
                loss=loss.item(),
                pos_loss=pos_loss.item(),
                yaw_loss=yaw_loss.item(),
                prob_loss=prob_loss.item(),
                time_loss=time_loss.item(),
            )

        return loss, stats_dict


    def train(
        self,
        *,
        n_epochs: Optional[int] = None,
        n_batches: Optional[int] = None,
        on_epoch_end: Callable[[], None] = None,
        on_batch_end: Callable[[], None] = None,
        log_interval: int = 500,
        log_rollouts_venv: Optional[vec_env.VecEnv] = None,
        log_rollouts_n_episodes: int = 5,
        progress_bar: bool = True,
        reset_tensorboard: bool = False,
        save_full_policy_path=None
    ):
        """Train with supervised learning for some number of epochs.

        Here an 'epoch' is just a complete pass through the expert data loader,
        as set by `self.set_expert_data_loader()`.

        Args:
            n_epochs: Number of complete passes made through expert data before ending
                training. Provide exactly one of `n_epochs` and `n_batches`.
            n_batches: Number of batches loaded from dataset before ending training.
                Provide exactly one of `n_epochs` and `n_batches`.
            on_epoch_end: Optional callback with no parameters to run at the end of each
                epoch.
            on_batch_end: Optional callback with no parameters to run at the end of each
                batch.
            log_interval: Log stats after every log_interval batches.
            log_rollouts_venv: If not None, then this VecEnv (whose observation and
                actions spaces must match `self.observation_space` and
                `self.action_space`) is used to generate rollout stats, including
                average return and average episode length. If None, then no rollouts
                are generated.
            log_rollouts_n_episodes: Number of rollouts to generate when calculating
                rollout stats. Non-positive number disables rollouts.
            progress_bar: If True, then show a progress bar during training.
            reset_tensorboard: If True, then start plotting to Tensorboard from x=0
                even if `.train()` logged to Tensorboard previously. Has no practical
                effect if `.train()` is being called for the first time.
        """
        it = EpochOrBatchIteratorWithProgress(
            self._demo_data_loader,
            n_epochs=n_epochs,
            n_batches=n_batches,
            on_epoch_end=on_epoch_end,
            on_batch_end=on_batch_end,
            progress_bar_visible=progress_bar,
        )

        if reset_tensorboard:
            self.tensorboard_step = 0

        batch_num = 0
        for batch, stats_dict_it in it:
            loss, stats_dict_loss = self._calculate_loss(batch["obs"], batch["acts"])

            self.optimizer.zero_grad()
            loss.backward()
            self.optimizer.step()

            if batch_num % log_interval == 0:
                for stats in [stats_dict_it, stats_dict_loss]:
                    for k, v in stats.items():
                        self.logger.record(f"bc/{k}", v)

                if(save_full_policy_path!=None):
                    index = save_full_policy_path.find('.pt')
                    tmp = save_full_policy_path[:index] + "_log" + str(math.floor(batch_num/log_interval)) + save_full_policy_path[index:]
                    self.save_policy(tmp)
                # TODO(shwang): Maybe instead use a callback that can be shared between
                #   all algorithms' `.train()` for generating rollout stats.
                #   EvalCallback could be a good fit:
                #   https://stable-baselines3.readthedocs.io/en/master/guide/callbacks.html#evalcallback
                if log_rollouts_venv is not None and log_rollouts_n_episodes > 0:
                    print("Going to evaluate student!!")

                    trajs = rollout.generate_trajectories(
                        self.policy,
                        log_rollouts_venv,
                        rollout.make_min_episodes(log_rollouts_n_episodes),
                    )
                    print("Student evaluated!!")
                    stats, traj_descriptors = rollout.rollout_stats(trajs)
                    self.logger.record("batch_size", len(batch["obs"]))
                    for k, v in stats.items():
                        if "return" in k and "monitor" not in k:
                            self.logger.record("rollout/" + k, v)
                self.logger.dump(self.tensorboard_step)
            batch_num += 1
            self.tensorboard_step += 1

    def save_policy(self, policy_path: types.AnyPath) -> None:
        """Save policy to a path. Can be reloaded by `.reconstruct_policy()`.

        Args:
            policy_path: path to save policy to.
        """
        th.save(self.policy, policy_path)
