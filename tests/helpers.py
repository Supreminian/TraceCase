from sdlc.service import Service


def scoped_service(db_path):
    """Keep existing single-project workflow tests explicitly scoped to a test project."""
    admin = Service(db_path)
    projects = admin.list_projects()
    project_id = projects[0]['id'] if projects else admin.create_project('测试项目', '', 'test-only')
    return Service(db_path, project_id=project_id)
