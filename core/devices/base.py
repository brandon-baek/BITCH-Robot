class Device:
    def __init__(self, device_id, name, caps, detail=''):
        self.id = device_id
        self.name = name
        self.caps = set(caps)
        self.detail = detail
        self.online = True
        self.hub = None

    def start(self):
        pass

    def active(self, capability):
        return self.online and capability in self.caps

    def info(self):
        return {
            'id': self.id, 'name': self.name, 'caps': sorted(self.caps),
            'online': self.online, 'detail': self.detail,
        }
