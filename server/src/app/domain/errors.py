class IllegalTransition(Exception):
    def __init__(self, machine: str, current: str, target: str) -> None:
        super().__init__(f"{machine}: cannot move from {current} to {target}")
        self.machine = machine
        self.current = current
        self.target = target
