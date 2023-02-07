import math
import random
from collections import namedtuple, deque
from itertools import count

import matplotlib
import numpy as np
import torch
from matplotlib import pyplot as plt
from torchdiffeq import odeint

is_ipython = 'inline' in matplotlib.get_backend()
from IPython import display

Transition = namedtuple("Transition", ('state', 'action', 'next_state', 'reward'))


class ReplayMemory:
    def __init__(self, capacity: int):
        self._memory = deque([], maxlen=capacity)

    def __len__(self):
        return len(self._memory)

    def push(self, state, action, next_state, reward):
        self._memory.append(Transition(state, action, next_state, reward))

    def sample(self, batch_size):
        return random.sample(self._memory, batch_size)

    def get_memory(self):
        return self._memory


class DQN(torch.nn.Module):
    def __init__(self, n_observation, n_actions):
        super(DQN, self).__init__()
        self.layer1 = torch.nn.Linear(n_observation, 128)
        self.layer2 = torch.nn.Linear(128, 128)
        self.layer3 = torch.nn.Linear(128, n_actions)

    def forward(self, x):
        x = torch.nn.functional.relu(self.layer1(x))
        x = torch.nn.functional.relu(self.layer2(x))
        x = torch.nn.functional.relu(self.layer3(x))
        return x


class ODENet(torch.nn.Module):
    def __init__(self, input_size):
        super(ODENet, self).__init__()
        self.net = torch.nn.Sequential(
            torch.nn.Linear(input_size, 100),
            torch.nn.Tanh(),
            torch.nn.Linear(100, 50),
            torch.nn.Tanh(),
            torch.nn.Linear(50, input_size))

        for m in self.net.modules():
            if isinstance(m, torch.nn.Linear):
                torch.nn.init.normal_(m.weight, mean=0, std=0.1)
                torch.nn.init.constant_(m.bias, val=0)

    def forward(self, t, y):
        net_output = self.net(y)
        return net_output


def get_action_from_ode_net(ode_net, state, params):
    # dy/dt = func(t, y), y(t[0]) = y0
    # da/ds = func(s, a), a(s[0]) = a0
    # ds/da = func(a, s), s(a[0]) = s0
    with torch.no_grad():
        policy = odeint(ode_net, torch.cat([state[:, 0, 0], torch.tensor([0])]), torch.linspace(-1, 1, 100))
        action = torch.reshape(policy[:, 2].argmax() / params.get("no_discretization_points"), (1,))
    return action


def select_action(state, params, env, steps_done, policy_net=None, ode_net=None):
    eps_threshold = params.get("eps_end") + (params.get("eps_start") - params.get("eps_end")) * \
                    math.exp(-1. * steps_done / params.get("eps_decay"))
    """print(f"eps: {eps_threshold} steps: {steps_done}")"""
    steps_done += 1
    with torch.no_grad():
        rand = random.random()
        if rand > eps_threshold and policy_net is not None:
            action = policy_net(state).max(1)[1].view(1, 1)
        elif rand > eps_threshold and ode_net is not None:
            action = get_action_from_ode_net(ode_net, state, params)
            """policy[torch.abs(policy[:, 1:3] - torch.flatten(state)).argmin()][0]"""
        else:
            action = torch.tensor(env.action_space.sample().reshape(1, 1, 1, ),
                                  device=params.get("device"),
                                  dtype=torch.float)
    return action, eps_threshold


def plot_durations(episode_epsilon_end, episode_training_error, episode_durations, show_result=False):
    durations = torch.tensor(episode_durations, dtype=torch.float)

    plt.figure(1)
    plt.clf()
    plt.subplot(121)
    plt.title('Length of an episode')
    plt.xlabel('Episode')
    plt.ylabel('Duration')
    plt.plot(durations.numpy())

    if len(durations) >= 100:
        means = durations.unfold(0, 100, 1).mean(1).view(-1)
        means = torch.cat((torch.zeros(99), means))
        plt.plot(means.numpy())

    plt.subplot(122)
    plt.title('Training Parameters')
    plt.xlabel('Episode')
    plt.ylabel('Epsilon')
    plt.plot(episode_epsilon_end, label="Epsilon")
    plt.gca().twinx().plot(episode_training_error, color='r', label="Training Error")
    plt.legend()
    plt.tight_layout()

    plt.pause(0.01)
    if is_ipython:
        if not show_result:
            display.display(plt.gcf())
            display.clear_output(wait=True)
        else:
            display.display(plt.gcf())


def optimize_model(policy_net, target_net, replay_memory, params, optimizer):
    if len(replay_memory) < params.get("batch_size"):
        return
    transitions = replay_memory.sample(params.get("batch_size"))
    batch = Transition(*zip(*transitions))

    non_final_mask = torch.tensor(tuple(map(lambda s: s is not None, batch.next_state)),
                                  device=params.get("device"), dtype=torch.bool)
    non_final_next_states = torch.cat([s for s in batch.next_state if s is not None])

    state_batch = torch.cat(batch.state)
    action_batch = torch.cat(batch.action)
    reward_batch = torch.cat(batch.reward)

    # Compute Q(s_t, a) - the model computes Q(s_t), then we select the
    # columns of actions taken. These are the actions which would've been taken
    # for each batch state according to policy_net
    state_action_values = policy_net(state_batch).gather(1, action_batch)

    # Compute V(s_{t+1}) for all next states.
    # Expected values of actions for non_final_next_states are computed based
    # on the "older" target_net; selecting their best reward with max(1)[0].
    # This is merged based on the mask, such that we'll have either the expected
    # state value or 0 in case the state was final.
    next_state_values = torch.zeros(params.get("batch_size"), device=params.get("device"))
    with torch.no_grad():
        next_state_values[non_final_mask] = target_net(non_final_next_states).max(1)[0]

    expected_state_action_values = (next_state_values * params.get("gamma")) + reward_batch

    criterion = torch.nn.SmoothL1Loss()
    loss = criterion(state_action_values, expected_state_action_values.unsqueeze(1))

    optimizer.zero_grad()
    loss.backward(retain_variables=True)
    torch.nn.utils.clip_grad_value_(policy_net.parameters(), 100)
    optimizer.step()
    return loss.item()


def run_training(env, replay_memory, target_net, policy_net, params):
    steps_done = 0
    episode_durations = []
    episode_epsilon_end = []
    episode_training_error = []
    device = params.get("device")
    optimizer = torch.optim.AdamW(policy_net.parameters(), lr=params.get("learning_rate"), amsgrad=True)
    for i_episode in range(params.get("no_episodes")):
        state, info = env.reset()
        state = torch.tensor(state, dtype=torch.float32, device=device).unsqueeze(0)
        for t in count():
            action, eps_threshold = select_action(state, params, env, steps_done, policy_net=policy_net)
            observation, reward, terminated, truncated, _ = env.step(action.item())
            reward = torch.tensor([reward], device=device)
            done = terminated or truncated

            if terminated:
                next_state = None
            else:
                next_state = torch.tensor(observation, dtype=torch.float32, device=device).unsqueeze(0)

            replay_memory.push(state, action, next_state, reward)
            state = next_state

            loss_value = optimize_model(policy_net, target_net, replay_memory, params, optimizer)

            target_net_state_dict = target_net.state_dict()
            policy_net_state_dict = policy_net.state_dict()

            for key in policy_net_state_dict:
                target_net_state_dict[key] = policy_net_state_dict[key] * params.get("tau") + \
                                             target_net_state_dict[key] * (1 - params.get("tau"))
            target_net.load_state_dict(target_net_state_dict)

            if done:
                episode_durations.append(t + 1)
                episode_epsilon_end.append(eps_threshold)
                episode_training_error.append(loss_value)
                plot_durations()
                break

    print('Complete')
    plot_durations(show_result=True)
    plt.ioff()
    plt.show()


def get_batch(x_axis_time, y_axis, z_axis, params):
    period_length = params.get("period_length")
    device = params.get("device")
    step_size = params.get("step_size", 1)
    batch_size = params.get("batch_size")

    index = torch.from_numpy(np.random.choice(np.arange(y_axis.size()[0] - period_length * step_size,
                                                        dtype=np.int64), size=batch_size, replace=False))
    y0_batch = torch.stack([x_axis_time[index], y_axis[index], z_axis[index]], dim=1)

    y_batch = torch.stack([y_axis[index + i] for i in range(0, period_length * step_size, step_size)], dim=0)
    x_batch = torch.stack([x_axis_time[index + i] for i in range(0, period_length * step_size, step_size)], dim=0)
    z_batch = torch.stack([z_axis[index + i] for i in range(0, period_length * step_size, step_size)], dim=0)
    output_batch = torch.stack([x_batch, y_batch, z_batch], dim=2)

    t_batch = x_axis_time[:period_length * step_size:step_size]
    return y0_batch.to(device), output_batch.to(device), t_batch.to(device)


def runNeuralODE(x_axis_time, y_axis, z_axis, params):
    plt.ion()
    no_dims = 3

    loss_list = []
    y_pred_prev = torch.stack([x_axis_time, y_axis, z_axis], dim=1)
    model = ODENet(no_dims)
    optimizer = torch.optim.RMSprop(model.parameters(), lr=1e-4)

    for itr in range(1, params.get("no_epochs") + 1):
        optimizer.zero_grad()
        y0_batch, y_batch, t_batch = get_batch(x_axis_time, y_axis, z_axis, params)
        torch.stack([y_axis for _ in range(params.get("batch_size"))])

        y_pred = odeint(model, y0_batch, t_batch).to(params.get("device"))

        loss_function = torch.nn.MSELoss()
        loss = loss_function(y_pred, y_batch)
        loss.backward(retain_graph=True)
        torch.nn.utils.clip_grad_value_(model.parameters(), 1)
        optimizer.step()

        if itr % 10 == 0:

            y_pred = odeint(model, y0_batch[0], x_axis_time)
            loss_list.append(loss.item())

            if y0_batch.shape[1] == 2:
                with torch.no_grad():
                    plt.figure(1)
                    plt.clf()
                    plt.plot(y_pred.numpy()[:, 1], y_pred.numpy()[:, 0], label="pred")
                    plt.plot(y_pred_prev.numpy()[:, 1], y_pred_prev.numpy()[:, 0],
                             label="prev pred")
                    plt.plot(x_axis_time.numpy(), y_axis.numpy(), label="true")
                    plt.legend()
                    plt.title(itr)
                    plt.draw()

            elif y0_batch.shape[1] == 3:
                y_true = torch.stack([x_axis_time, y_axis, z_axis], dim=1)
                with torch.no_grad():
                    ax = plt.figure(1).add_subplot(projection='3d')
                    ax.scatter(y_pred_prev[:, 0].numpy(), y_pred_prev[:, 1].numpy(), y_pred_prev[:, 2].numpy(),
                               cmap=matplotlib.cm.coolwarm, label="prev pred")
                    ax.scatter(y_pred[:, 0].numpy(), y_pred[:, 1].numpy(), y_pred[:, 2].numpy(),
                               cmap=matplotlib.cm.coolwarm, label="pred")
                    ax.scatter(y_true[:, 0].numpy(), y_true[:, 1].numpy(), y_true[:, 2].numpy(),
                               cmap=matplotlib.cm.coolwarm, label="true")
                    ax.set_xlabel("time")
                    ax.set_ylabel("y (x^3)")
                    ax.set_zlabel("z (sin(y))")
                    plt.legend()
                    plt.title(itr)
                    plt.draw()

            if itr != params.get("no_epchs"):
                plt.pause(0.0001)
                plt.clf()
            else:
                plt.savefig("training_progress.png")
            y_pred_prev = y_pred
    # plt.close()
    # plt.figure(1)
    # plt.plot(loss_list)
    # plt.title("Loss")
    # plt.show()


def runNeuralODE_gym(env, replay_memory, params):
    device = params.get("device")

    # plt.ion()
    no_dims = 3
    no_dp = params.get("no_discretization_points")

    ode_net = ODENet(no_dims)
    optimizer = torch.optim.RMSprop(ode_net.parameters(), lr=1e-4)
    prev_state_values = torch.zeros((no_dp, no_dims))

    loss_list, reward_list = [], []
    for epoch in range(1, params.get("no_epochs") + 1):
        state, info = env.reset()
        state = torch.tensor(state, dtype=torch.float32, device=device)[:, None, None]
        reward_total_per_epoch = 0
        steps = 0
        done = False
        while not done:
            action, eps_threshold = select_action(state, params, env, epoch, ode_net=ode_net)
            for i in range(1):
                observation, reward, terminated, truncated, _ = env.step(action.numpy())
                done = (terminated or truncated)
                reward_total_per_epoch += reward
                reward = torch.tensor([reward], device=device)

                if done:
                    print(f"E{epoch}: Done after {steps} steps, terminated: {terminated}, truncated: {truncated}," +
                          f" reward {reward_total_per_epoch:.2f}")
                    steps = 0
                    next_state = None
                    reward_list.append(reward_total_per_epoch)
                    break
                else:
                    next_state = torch.reshape(torch.tensor(observation, dtype=torch.float32, device=device), (2, 1, 1))
                    steps += 1

                replay_memory.push(state, action, next_state, reward)
                state = next_state

        if len(replay_memory) > params.get("batch_size"):
            transitions = replay_memory.sample(params.get("batch_size"))
            batch = Transition(*zip(*transitions))

            state_action_values = []
            for state, reward, action in zip(batch.state, batch.reward, batch.action):
                state_values = odeint(ode_net, torch.cat([state[:, 0, 0], reward]), torch.linspace(-1, 1, 100))
                q_value = state_values[int(action.flatten()[0] * no_dp)][2][None]
                state_action_values.append(q_value)

            expected_state_action_values = []
            for next_state, reward in zip(batch.next_state, batch.reward):
                if next_state is not None:
                    state_values = odeint(ode_net, torch.cat([next_state[:, 0, 0], reward]), torch.linspace(-1, 1, 100))
                    q_value_next_state = state_values[:, 2].max()[None]
                    expected_state_action_values.append((q_value_next_state * params.get("gamma")) + reward)
                else:
                    expected_state_action_values.append(reward)

            state_action_values = torch.cat(state_action_values)
            expected_state_action_values = torch.cat(expected_state_action_values)

            criterion = torch.nn.MSELoss()
            loss = criterion(state_action_values, expected_state_action_values)

            optimizer.zero_grad()

            loss.backward()
            torch.nn.utils.clip_grad_value_(ode_net.parameters(), 100)
            optimizer.step()

            loss_list.append(loss.item())

        if epoch % 10 == 0:
            print(f"E{epoch}: Loss {loss.item():.6f}, Esp: {eps_threshold:.2f}")
            state_values = odeint(ode_net, torch.tensor([-1, -1.2, 0]), torch.linspace(-1, 1, 100))
            transitions = replay_memory.get_memory()
            full_batch = Transition(*zip(*transitions))

            with torch.no_grad():
                ax = plt.figure(1).add_subplot(projection='3d')
                #ax.scatter(state_values[:, 0].numpy(), state_values[:, 1].numpy(), state_values[:, 2].numpy(),
                #           cmap=matplotlib.cm.coolwarm, label="policy")
                #ax.scatter(prev_state_values[:, 0].numpy(), prev_state_values[:, 1].numpy(),
                #           prev_state_values[:, 2].numpy(),
                #           cmap=matplotlib.cm.coolwarm, label="prev policy")
                ax.scatter(torch.tensor(full_batch.action).numpy(),
                           torch.cat(full_batch.state, 1).numpy()[0],
                           torch.cat(full_batch.state, 1).numpy()[1],
                           cmap=matplotlib.cm.coolwarm, label="true")
                ax.set_xlabel("action")
                ax.set_ylabel("state 1: position")
                ax.set_zlabel("state 2: velocity")
                plt.legend()
                plt.title(epoch)
                prev_state_values = state_values

            if epoch != params.get("no_epchs") and False:
                plt.pause(0.0001)

            plt.savefig("training_progress.png")
            plt.clf()

            plt.figure(1)
            plt.plot(loss_list)
            plt.savefig("training_loss.png")
            plt.clf()
